import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/** Keeps a render-time failure visible and recoverable instead of leaving a blank root. */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Life Context dashboard render failure", error, errorInfo);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-screen" role="alert">
          <div className="error-card">
            <div className="warning-icon" aria-hidden="true">
              !
            </div>
            <div>
              <span className="section-kicker">Dashboard unavailable</span>
              <h1>The dashboard needs to be reloaded.</h1>
              <p>
                A display error interrupted this view. Your captured context
                remains on the configured server.
              </p>
            </div>
            <button
              className="primary-button"
              onClick={() => window.location.reload()}
              type="button"
            >
              Reload dashboard
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
