/** Exavalu Data Processing Studio design tokens.
 *
 * Neutral first: cool greys carry the interface, one cobalt accent marks the primary
 * action, the active step and focus. Green, amber and red mean success, warning and
 * error only -- never decoration -- so a status reads at a glance. The Exavalu mark is
 * the only place the brand red appears. See design-system/exavalu-data-cleaning-studio.
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // 24-inch and larger monitors: 1920 (FHD) and 2560 (QHD) wide.
      screens: { "3xl": "1920px", "4xl": "2400px" },
      colors: {
        // Cobalt: primary action, active step, focus and selection. ~70% saturation.
        brand: {
          50: "#EFF4FE",
          100: "#DCE6FB",
          200: "#BACDF6",
          300: "#8CABEE",
          400: "#5B85E2",
          500: "#3A69D8",
          600: "#2556C7",
          700: "#1E46A3",
          800: "#1B3A82",
          900: "#182F64",
        },
        // Errors and failed states only.
        danger: {
          50: "#FEF3F2",
          100: "#FEE4E2",
          200: "#FECDCA",
          300: "#FDA29B",
          500: "#E5483D",
          600: "#D92D20",
          700: "#B42318",
          800: "#912018",
        },
        // Cool neutral greys, one family throughout.
        ink: {
          900: "#14171C",
          800: "#1F242B",
          700: "#353C45",
          600: "#4C5562",
          500: "#67707D",
          400: "#98A0AB",
          300: "#C8CDD5",
          200: "#E2E5EA",
          100: "#EFF1F4",
          50: "#F6F7F9",
        },
        // Charcoal sidebar.
        nav: { DEFAULT: "#15181D", raised: "#1E2228", line: "#2A2F37" },
      },
      fontFamily: {
        display: ["IBM Plex Sans", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        sans: ["IBM Plex Sans", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Consolas", "monospace"],
      },
      fontSize: {
        // Type scale: caption / table / body / card heading / section / page title
        caption: ["12px", { lineHeight: "16px" }],
        table: ["13px", { lineHeight: "20px" }],
        body: ["14px", { lineHeight: "20px" }],
        card: ["15px", { lineHeight: "22px", fontWeight: "600" }],
        section: ["18px", { lineHeight: "26px", fontWeight: "600" }],
        page: ["28px", { lineHeight: "36px", fontWeight: "600", letterSpacing: "-0.01em" }],
      },
      borderRadius: { DEFAULT: "5px", md: "6px", lg: "7px", xl: "9px", "2xl": "12px" },
      boxShadow: {
        card: "0 1px 2px rgba(20, 23, 28, 0.04)",
        pop: "0 12px 32px -4px rgba(20, 23, 28, 0.16), 0 2px 6px rgba(20, 23, 28, 0.06)",
        focus: "0 0 0 3px rgba(37, 86, 199, 0.28)",
      },
      keyframes: {
        "fade-in": { from: { opacity: 0, transform: "translateY(4px)" }, to: { opacity: 1, transform: "none" } },
        "scale-in": { from: { opacity: 0, transform: "scale(0.96)" }, to: { opacity: 1, transform: "none" } },
        shimmer: { "100%": { transform: "translateX(100%)" } },
        "progress-stripes": { from: { backgroundPosition: "24px 0" }, to: { backgroundPosition: "0 0" } },
        "draw-check": { from: { strokeDashoffset: 24 }, to: { strokeDashoffset: 0 } },
        "slide-in-right": { from: { transform: "translateX(16px)", opacity: 0 }, to: { transform: "none", opacity: 1 } },
        indeterminate: { "0%": { left: "-35%" }, "100%": { left: "100%" } },
      },
      animation: {
        "fade-in": "fade-in 200ms ease-out both",
        "scale-in": "scale-in 180ms ease-out both",
        shimmer: "shimmer 1.4s infinite",
        stripes: "progress-stripes 1s linear infinite",
        "draw-check": "draw-check 400ms ease-out 100ms both",
        "slide-in-right": "slide-in-right 200ms ease-out both",
        indeterminate: "indeterminate 1.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
