import { Component } from 'react'

// Catches render-time exceptions (e.g. a null-deref from an unexpected API
// shape) and shows a recoverable message instead of letting React unmount
// the whole tree — which is what produced the "screen goes black, only a
// reload fixes it" symptom (TopBar was reading `n.audio.lastTriggerAt`
// without a null-guard, and a single missed poll cycle nulled `audio` out).
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('Acoustic Base — render error caught by boundary:', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{
          height: '100vh', display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 12,
          background: 'var(--bg-base)', color: 'var(--text-primary)',
          fontSize: 14, textAlign: 'center', padding: 24,
        }}>
          <div style={{ fontSize: 28 }}>⚠</div>
          <div style={{ fontWeight: 600 }}>Something went wrong rendering the dashboard</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 12, maxWidth: 480 }}>
            {this.state.error?.message ?? String(this.state.error)}
          </div>
          <button
            className="btn btn-primary"
            onClick={() => this.setState({ error: null })}
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
