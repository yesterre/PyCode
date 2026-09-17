import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react'
import { messageOf } from '../api/client'

export function Card({children, className = ''}: {children: ReactNode; className?: string}) {
  return <section className={`rounded-xl border border-slate-700/70 bg-slate-900/70 p-5 shadow-lg shadow-black/10 ${className}`}>{children}</section>
}
export function Button({children, className = '', variant = 'primary', ...props}:
  ButtonHTMLAttributes<HTMLButtonElement> & {variant?: 'primary'|'secondary'|'quiet'}) {
  const style = variant === 'primary' ? 'bg-cyan-400 text-slate-950 hover:bg-cyan-300' :
    variant === 'secondary' ? 'bg-slate-800 text-slate-100 hover:bg-slate-700 border border-slate-600' :
      'text-cyan-300 hover:bg-slate-800'
  return <button {...props} className={`inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${style} ${className}`}>{children}</button>
}
export function Badge({children, tone = 'neutral'}: {children: ReactNode; tone?: 'neutral'|'good'|'bad'|'working'}) {
  const color = {neutral: 'bg-slate-700/70 text-slate-200', good: 'bg-emerald-400/15 text-emerald-300',
    bad: 'bg-rose-400/15 text-rose-300', working: 'bg-amber-400/15 text-amber-200'}[tone]
  return <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-semibold ${color}`}>{children}</span>
}
export function StatusBadge({status}: {status: string}) {
  const tone = status === 'ready' || status === 'completed' || status === 'live' ? 'good' :
    status === 'failed' || status === 'gap' || status === 'unavailable' ? 'bad' :
      status === 'running' || status === 'queued' || status === 'indexing' || status === 'preparing' || status === 'reconnecting' ? 'working' : 'neutral'
  return <Badge tone={tone}>{status.replaceAll('_', ' ')}</Badge>
}
export function Spinner({label = 'Loading…'}: {label?: string}) {
  return <div role="status" className="flex items-center gap-3 text-sm text-slate-400"><span className="size-4 animate-spin rounded-full border-2 border-slate-600 border-t-cyan-300" />{label}</div>
}
export function EmptyState({title, detail}: {title: string; detail?: string}) {
  return <div className="rounded-lg border border-dashed border-slate-700 px-5 py-8 text-center"><p className="font-semibold text-slate-200">{title}</p>{detail && <p className="mt-1 text-sm text-slate-400">{detail}</p>}</div>
}
export function ErrorState({error}: {error: unknown}) {
  return <div role="alert" className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{messageOf(error)}</div>
}
export function Field({label, children, hint}: {label: string; children: ReactNode; hint?: string}) {
  return <div><label className="block text-sm font-medium text-slate-200">{label}{children}</label>{hint && <p className="mt-1 text-xs text-slate-400">{hint}</p>}</div>
}
export const inputClass = 'mt-2 w-full rounded-lg border border-slate-600 bg-slate-950 px-3 py-2.5 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-cyan-400 focus:ring-1 focus:ring-cyan-400'
export function SectionHeading({title, subtitle, action}: {title: string; subtitle?: string; action?: ReactNode}) {
  return <div className="mb-4 flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold text-white">{title}</h2>{subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}</div>{action}</div>
}
export function Metric({label, value}: {label: string; value: ReactNode}) {
  return <div className="rounded-lg border border-slate-700/60 bg-slate-950/50 p-3"><div className="text-xs uppercase tracking-wide text-slate-400">{label}</div><div className="mt-1 text-lg font-semibold text-slate-100">{value}</div></div>
}
export function PageShell({children}: HTMLAttributes<HTMLDivElement>) {
  return <div className="min-h-screen bg-[radial-gradient(circle_at_top_left,_#123348_0%,_#10151d_35%,_#10151d_100%)]"><div className="mx-auto max-w-6xl px-5 py-8 sm:px-8">{children}</div></div>
}
