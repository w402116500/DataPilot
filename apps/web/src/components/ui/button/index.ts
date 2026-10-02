import type { VariantProps } from "class-variance-authority";
import { cva } from "class-variance-authority";

export { default as Button } from "./Button.vue";

export const buttonVariants = cva(
  "inline-flex h-9 items-center justify-center gap-2 whitespace-nowrap rounded-lg border border-transparent px-4 py-2 text-sm font-medium transition-all focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 active:translate-y-px [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground shadow-[0_6px_18px_rgb(217_75_50/18%)] hover:bg-primary/90",
        destructive: "bg-destructive text-destructive-foreground shadow-[0_6px_18px_rgb(217_84_93/16%)] hover:bg-destructive/90",
        outline: "border-input/80 bg-workspace-surface-inset text-foreground hover:border-ring/60 hover:bg-workspace-surface-hover hover:text-foreground",
        secondary: "border-workspace-border bg-secondary/80 text-secondary-foreground hover:bg-secondary",
        ghost: "hover:bg-workspace-surface-hover hover:text-foreground",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 rounded-md px-3 text-xs",
        icon: "h-9 w-9 px-0",
        "icon-sm": "h-8 w-8 px-0",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export type ButtonVariants = VariantProps<typeof buttonVariants>;
