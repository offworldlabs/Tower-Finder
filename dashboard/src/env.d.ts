declare module "*.css" {}

/* Allow CSS custom properties in style objects */
import "react";
declare module "react" {
  interface CSSProperties {
    [key: `--${string}`]: string | number | undefined;
  }
}
