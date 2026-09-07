import type { Config } from "tailwindcss";

// Design 1a's dark token palette -- see
// design_handoff_karting_telemetry/README.md's "Design tokens" table for
// where these values come from; change them there and here together.
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#0b0d0f",
        surface: "#0d1114",
        raised: "#101417",
        rowalt: "#0f1316",
        selected: "#181e22",
        ink: "#eef0f1",
        ink2: "#c9cfd4",
        muted: "#8c959c",
        faint: "#6d767d",
        hairline: "rgba(255,255,255,.10)",
        accent: "#ff3b1f",
        gain: "#2fd07a",
        loss: "#ff4a3d",
        reference: "#b06cff",
        theoretical: "#ffd23d",
      },
      fontFamily: {
        sans: ["Archivo", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
