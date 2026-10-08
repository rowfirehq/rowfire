/* The mark: a row, firing.
 *
 * Two stacked bars read as rows at a glance; an ember lifts off the end of the
 * top one. That is the product's whole mechanic in one shape -- a row matched,
 * and something left because of it.
 *
 * Drawn in a 24-unit box with solid fills rather than strokes, because strokes
 * thin out and disappear at favicon size. It uses currentColor, so it takes
 * the brand colour from whatever it sits in.
 */
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {/* The rows, which are the subject. They get the width and the weight;
          the ember is the event, and events are small. */}
      <rect x="2" y="12.6" width="13" height="3.1" rx="1.55" />
      <rect x="2" y="18.2" width="8.5" height="3.1" rx="1.55" opacity="0.4" />

      {/* The ember, lifting off the right end of the top row. It leans, because
          a symmetrical one reads as a water droplet -- the lean is what makes
          it a flame at 16 pixels. The spark trails up and away from the tip. */}
      <path d="M18.9 3.5c-.3 1.9.6 2.5 1.3 3.5.6.9 1 1.6 1 2.5a3 3 0 0 1-6 0c0-2 2.3-2.8 3.7-6z" />
      <circle cx="20.6" cy="1.5" r="0.85" opacity="0.5" />
    </svg>
  );
}
