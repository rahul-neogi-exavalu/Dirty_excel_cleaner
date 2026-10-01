/** Exavalu Data Cleaning Studio design tokens.
 *
 * Forest green marks the primary action, the active step and focus; errors use the
 * separate danger scale, and everything else sits on a green-tinted neutral scale.
 * Spacing uses Tailwind's 4px scale restricted in practice to 1,2,3,4,6,8,10,12
 * (4/8/12/16/24/32/40/48 px).
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Forest green: primary action, active step, focus and selection.
        brand: {
          50: "#EEF7F1",
          100: "#D9EFE1",
          200: "#B4DEC2",
          300: "#82C59B",
          400: "#4FA872",
          500: "#2A8C55",
          600: "#1D7A46",
          700: "#17623A",
          800: "#124D2E",
          900: "#0D3A23",
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
        // Neutrals with a faint green cast so greys sit comfortably beside the brand.
        ink: {
          900: "#0F1E17",
          800: "#1B2B23",
          700: "#33433A",
          600: "#4B5A52",
          500: "#67766D",
          400: "#97A59C",
          300: "#C8D3CC",
          200: "#E1E8E3",
          100: "#EEF2EF",
          50: "#F5F8F6",
        },
        nav: { DEFAULT: "#0E2A1D", raised: "#173B29", line: "#21483A" },
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
      borderRadius: { DEFAULT: "6px", md: "8px", lg: "10px", xl: "14px", "2xl": "18px" },
      boxShadow: {
        card: "0 1px 2px rgba(15, 30, 23, 0.03), 0 4px 16px -8px rgba(15, 30, 23, 0.06)",
        pop: "0 12px 32px rgba(15, 30, 23, 0.14), 0 2px 6px rgba(15, 30, 23, 0.06)",
        focus: "0 0 0 3px rgba(29, 122, 70, 0.25)",
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
