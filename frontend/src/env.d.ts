/// <reference types="vite/client" />

declare module "*.css" {}

interface ImportMetaEnv {
  readonly VITE_ENABLE_TEST_RADAR?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/* Leaflet internal icon URL fix */
declare module "leaflet" {
  namespace Icon {
    interface Default {
      _getIconUrl?: string;
    }
  }
}

/* Allow CSS custom properties in style objects */
import "react";
declare module "react" {
  interface CSSProperties {
    [key: `--${string}`]: string | number | undefined;
  }
}
