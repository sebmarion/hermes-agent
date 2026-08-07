import { createContext, type ReactNode, useContext, useState } from 'react'

/** ... */
const GroupContext = createContext<GroupContextShape>({ kind: 'grid' })

function useGroupRegistryContext(): GroupContextShape {
  return useContext(GroupContext)
}

export interface GroupContextShape {
  kind: 'grid' | 'tabstrip'
}

interface PaneProps {
  children: ReactNode
  defaultOpen?: boolean
  disabled?: boolean
  forceCollapsed?: boolean
  hoverReveal?: boolean
  id?: string
  key?: string
  maxWidth?: number | string
  minWidth?: number | string
  onOverlayActiveChange?: (active: boolean) => void
  resizable?: boolean
  side?: 'left' | 'right'
  width?: number | string
}

export function Pane({ children, id, width, ..._rest }: PaneProps) {
  return (
    <aside className={`pane ${id ?? ''}`} data-pane={id} style={width ? { width: `${width}` } : undefined}>
      {children}
    </aside>
  )
}

export function PaneMain({ children }: { children: ReactNode }) {
  return <main className="pane-main">{children}</main>
}

interface AppShellProps {
  children: ReactNode
  leftStatusbarItems?: readonly { key: string }[]
  leftTitlebarTools?: readonly { key: string }[]
  mainOverlays?: ReactNode
  onOpenSettings?: () => void
  overlays?: ReactNode
  previewPaneOpen?: boolean
  statusbarItems?: readonly { key: string }[]
  terminalPaneOpen?: boolean
  titlebarTools?: readonly { key: string }[]
}

export function AppShell({ children, ..._props }: AppShellProps) {
  return <div className="app-shell">{children}</div>
}

interface GroupHandle<T> {
  flat: { left: T[]; right: T[] }
  set: (group: T[]) => void
}

export function useGroupRegistry<T>(): GroupHandle<T> {
  const [group] = useState<T[]>([])

  return {
    flat: { left: group, right: group },
    set: () => {}
  }
}
