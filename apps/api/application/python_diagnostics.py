"""Extract safe failure facts from an untrusted Python process traceback."""

from __future__ import annotations

import io
import re
import tokenize

from contracts.python_diagnostics import PythonDiagnostic

_FRAME = re.compile(r'File "(?:[^"\n]*/)?analysis\.py", line ([1-9][0-9]{0,6})')
_EXCEPTION = re.compile(r"^([A-Za-z_][A-Za-z_0-9]{0,79}):(?: (.*))?$")
_SAFE_MESSAGES = {
    "KeyError": "访问的键或列不存在；异常中的数据值已省略。",
    "IndexError": "索引超出范围；请核对当前集合长度。",
    "AttributeError": "当前对象没有请求的属性。",
    "NameError": "使用了尚未定义的名称。",
    "UnboundLocalError": "局部变量尚未赋值。",
    "ZeroDivisionError": "除数为零。",
    "ModuleNotFoundError": "沙箱中不存在请求的模块；不能联网安装。",
    "ImportError": "模块导入失败；请核对沙箱已有依赖。",
    "FileNotFoundError": "请求的文件不存在；请核对本 Run 的受控输入。",
}
_STANDARD_MESSAGES = re.compile(
    r"^(?:invalid syntax|unexpected indent|unindent does not match any outer indentation level|"
    r"expected an indented block|unexpected EOF while parsing|"
    r"All arrays must be of the same length|"
    r"x and y must have same first dimension|"
    r"could not convert string to float|"
    r"unsupported operand type\(s\) for|"
    r"missing [0-9]+ required positional argument|"
    r".* got an unexpected keyword argument)"
)


def safe_code_line(line: str) -> str | None:
    """Keep Python structure, excluding literals, comments and invalid fragments."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line.strip()).readline))
        # Python 3.12 tokenizes f-string content separately from STRING. Omit
        # this frame rather than release literal values or interpolated data.
        if any(token.type == tokenize.FSTRING_START for token in tokens):
            return None
        safe = [
            (token.type, "'[value]'" if token.type == tokenize.STRING else token.string)
            for token in tokens
            if token.type not in {tokenize.COMMENT, tokenize.NUMBER}
        ]
        if any(token.type == tokenize.ERRORTOKEN for token in tokens):
            return None
        return tokenize.untokenize(safe).strip()[:300] or None
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def python_diagnostic(stderr: str, *, exit_code: int, script: str = "") -> PythonDiagnostic:
    """Unknown/truncated output still produces a diagnostic, never raw fallback."""
    tail = stderr[-16_384:]
    frames = [int(match.group(1)) for match in _FRAME.finditer(tail)]
    exception = next(
        (match for line in reversed(tail.splitlines()) if (match := _EXCEPTION.fullmatch(line))),
        None,
    )
    exception_type = exception.group(1) if exception else None
    raw_message = exception.group(2) or "" if exception else ""
    message = _SAFE_MESSAGES.get(exception_type, "异常消息含运行时内容，已省略。")
    # Only fixed standard wording is released; arbitrary values and custom messages are not.
    standard = _STANDARD_MESSAGES.match(raw_message)
    if standard and exception_type in {
        "SyntaxError",
        "IndentationError",
        "TypeError",
        "ValueError",
    }:
        message = standard.group(0)
        if "unexpected keyword argument" in message:
            message = "got an unexpected keyword argument"
    if exception_type is None:
        message = "进程未成功退出，未获得可解析的 Python 异常。"
    line_number = frames[-1] if frames else None
    lines = script.splitlines()
    code_line = (
        safe_code_line(lines[line_number - 1])
        if line_number is not None and line_number <= len(lines)
        else None
    )
    return PythonDiagnostic(
        exception_type=exception_type,
        message=message[:500],
        line_number=line_number,
        code_line=code_line,
        traceback_tail=[f"analysis.py:{line}" for line in frames[-5:]],
        exit_code=exit_code,
        message_redacted=message != raw_message,
    )
