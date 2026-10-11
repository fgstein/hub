import type { JSX } from "solid-js"

type P = { class?: string; size?: number } & JSX.SvgSVGAttributes<SVGSVGElement>

function Svg(props: P & { children: JSX.Element }) {
  const size = () => props.size ?? 14
  return (
    <svg
      width={size()}
      height={size()}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      class={props.class}
      aria-hidden="true"
    >
      {props.children}
    </svg>
  )
}

export const IconPause = (p: P) => (
  <Svg {...p}>
    <path d="M5.5 3.5v9M10.5 3.5v9" />
  </Svg>
)
export const IconPlay = (p: P) => (
  <Svg {...p}>
    <path d="M5 3.5v9l7-4.5z" fill="currentColor" stroke-width="1" />
  </Svg>
)
export const IconStop = (p: P) => (
  <Svg {...p}>
    <rect x="4" y="4" width="8" height="8" rx="1" fill="currentColor" stroke="none" />
  </Svg>
)
export const IconRelaunch = (p: P) => (
  <Svg {...p}>
    <path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.5h-2.5" />
  </Svg>
)
export const IconRestart = (p: P) => (
  <Svg {...p}>
    <path d="M3 8a5 5 0 1 0 1.5-3.6M3 2.5v2.5h2.5" />
  </Svg>
)
export const IconChevronDown = (p: P) => (
  <Svg {...p}>
    <path d="M4 6l4 4 4-4" />
  </Svg>
)
export const IconChevronRight = (p: P) => (
  <Svg {...p}>
    <path d="M6 4l4 4-4 4" />
  </Svg>
)
export const IconPlus = (p: P) => (
  <Svg {...p}>
    <path d="M8 3v10M3 8h10" />
  </Svg>
)
export const IconClose = (p: P) => (
  <Svg {...p}>
    <path d="M4 4l8 8M12 4l-8 8" />
  </Svg>
)
export const IconCopy = (p: P) => (
  <Svg {...p}>
    <rect x="5.5" y="5.5" width="7" height="7" rx="1.5" />
    <path d="M10.5 5.5V4.5a1 1 0 0 0-1-1h-5a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1h1" />
  </Svg>
)
export const IconDownload = (p: P) => (
  <Svg {...p}>
    <path d="M8 2.5v8M4.5 7.5L8 11l3.5-3.5M3 13.5h10" />
  </Svg>
)
export const IconMaximize = (p: P) => (
  <Svg {...p}>
    <path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9 7M2.5 13.5L7 9" />
  </Svg>
)
export const IconMinimize = (p: P) => (
  <Svg {...p}>
    <path d="M13.5 6.5h-4v-4M2.5 9.5h4v4M9.5 6.5L14 2M6.5 9.5L2 14" />
  </Svg>
)
export const IconSearch = (p: P) => (
  <Svg {...p}>
    <circle cx="7" cy="7" r="4" />
    <path d="M10 10l3.5 3.5" />
  </Svg>
)
export const IconAgent = (p: P) => (
  <Svg {...p}>
    <rect x="3" y="5" width="10" height="8" rx="2" />
    <path d="M8 2.5V5M6 9h.01M10 9h.01" stroke-width="2" />
  </Svg>
)
export const IconCheck = (p: P) => (
  <Svg {...p}>
    <path d="M3.5 8.5l3 3 6-7" />
  </Svg>
)
export const IconTrash = (p: P) => (
  <Svg {...p}>
    <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.5 8.5h6l.5-8.5" />
  </Svg>
)
export const IconArrowDown = (p: P) => (
  <Svg {...p}>
    <path d="M8 3v10M4 9l4 4 4-4" />
  </Svg>
)
export const IconRefresh = (p: P) => (
  <Svg {...p}>
    <path d="M13 3v3h-3M3 13v-3h3M12.5 6A5 5 0 0 0 4 4.5M3.5 10A5 5 0 0 0 12 11.5" />
  </Svg>
)
export const IconReset = (p: P) => (
  <Svg {...p}>
    <path d="M3 3v3h3M3.5 6A5 5 0 1 1 3 8" />
  </Svg>
)
export const IconSun = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="2.5" />
    <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" />
  </Svg>
)
export const IconMoon = (p: P) => (
  <Svg {...p}>
    <path d="M13 9.5A5.5 5.5 0 1 1 6.5 3a4.5 4.5 0 0 0 6.5 6.5z" />
  </Svg>
)
export const IconMonitor = (p: P) => (
  <Svg {...p}>
    <rect x="2" y="3" width="12" height="8" rx="1.5" />
    <path d="M6 13.5h4M8 11v2.5" />
  </Svg>
)
export const IconTerminal = (p: P) => (
  <Svg {...p}>
    <path d="M3 4.5l3 3-3 3M8 11h5" />
  </Svg>
)
export const IconPower = (p: P) => (
  <Svg {...p}>
    <path d="M8 2.5v5.5M4.8 4.6a4.5 4.5 0 1 0 6.4 0" />
  </Svg>
)
export const IconSpinner = (p: P) => (
  <Svg {...p} class={`animate-spin ${p.class ?? ""}`}>
    <path d="M8 2a6 6 0 1 0 6 6" />
  </Svg>
)
