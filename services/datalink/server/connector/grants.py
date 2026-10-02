"""Resolve a Build-bound capability over the private control-plane boundary."""

from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from contracts.datalink import (
    DataLinkConnectionGrantConsumeRequest,
    DataLinkConnectionGrantRead,
    DataLinkErrorCode,
    DataLinkRebuildRequest,
)
from pydantic import ValidationError

from server.config import Settings
from server.connector.base import ConnectorError


class ConnectionGrantResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def consume(
        self, request: DataLinkRebuildRequest, build_id: str
    ) -> DataLinkConnectionGrantRead:
        token = self.settings.service_token
        parsed = urlsplit(self.settings.control_url)
        try:
            loopback = (
                parsed.hostname == "localhost" or ip_address(parsed.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            not token
            or not token.get_secret_value()
            or request.connection_grant is None
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and loopback))
        ):
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "Connection grant control is unavailable"
            )
        payload = DataLinkConnectionGrantConsumeRequest(
            token=request.connection_grant,
            rebuild_key=request.rebuild_key,
            build_id=build_id,
            datasource_id=request.datasource_id,
            schema_revision=request.schema_revision,
            connection_revision=request.connection_revision,
        )
        body = payload.model_dump(mode="json")
        body["token"] = payload.token.get_secret_value()
        try:
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    self.settings.control_url.rstrip("/") + "/internal/connection-grants/consume",
                    headers={"Authorization": "Bearer " + token.get_secret_value()},
                    json=body,
                )
                response.raise_for_status()
                envelope = response.json()
                grant = DataLinkConnectionGrantRead.model_validate(envelope["data"])
        except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError):
            raise ConnectorError(
                DataLinkErrorCode.BUILD_FAILED, "Connection grant was rejected"
            ) from None
        if (
            grant.datasource_id != request.datasource_id
            or grant.schema_revision != request.schema_revision
            or grant.connection_revision != request.connection_revision
            or grant.source_type != request.source_type.value
        ):
            raise ConnectorError(
                DataLinkErrorCode.REVISION_INVALID, "Connection grant revision is stale"
            )
        return grant
