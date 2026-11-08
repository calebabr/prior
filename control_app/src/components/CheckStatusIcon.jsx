// Distinct shapes per status (check circle / warning triangle / cross circle) so meaning never rests on colour alone.
export default function CheckStatusIcon({ status, className = '' }) {
  const common = {
    className: `arm-glyph ${status} ${className}`.trim(),
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': true,
  };
  if (status === 'pass') {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" />
        <path d="M8 12.5l2.7 2.7L16 9.5" />
      </svg>
    );
  }
  if (status === 'warn') {
    return (
      <svg {...common}>
        <path d="M12 3.5l9.5 16.5h-19z" />
        <path d="M12 10v4.5M12 17.4v.1" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="9" />
      <path d="M9 9l6 6M15 9l-6 6" />
    </svg>
  );
}
