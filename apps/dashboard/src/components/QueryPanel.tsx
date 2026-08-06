import { FormEvent, KeyboardEvent } from "react";
import { Icon } from "./Icon";
import { SourceIcon } from "./SourceIcon";
import type { AnswerCard, RankedResult } from "../types";

interface QueryPanelProps {
  query: string;
  onQueryChange: (value: string) => void;
  onSubmit: () => void;
  answer: AnswerCard;
  results: RankedResult[];
  loading: boolean;
}

const suggestedQueries = [
  "What changed this week?",
  "Where did I leave off?",
  "What needs my attention?",
];

export function QueryPanel({
  query,
  onQueryChange,
  onSubmit,
  answer,
  results,
  loading,
}: QueryPanelProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit();
  }

  function handleQueryKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (!loading && query.trim()) onSubmit();
    }
  }

  return (
    <section
      aria-labelledby="query-heading"
      className="query-column"
      id="overview"
    >
      <div className="query-intro">
        <div className="section-kicker">
          <Icon name="sparkle" size={15} /> Ask your context
        </div>
        <h2 id="query-heading">Find the thread.</h2>
        <p>
          Ask naturally. Life Context brings together the moments that connect.
        </p>
      </div>

      <form className="query-form" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="context-query">
          Ask your context
        </label>
        <div className="query-input-wrap">
          <Icon name="search" size={18} />
          <textarea
            aria-describedby="query-helper"
            id="context-query"
            onChange={(event) => onQueryChange(event.target.value)}
            onKeyDown={handleQueryKeyDown}
            placeholder="What are you trying to remember?"
            rows={3}
            value={query}
          />
          <span className="query-shortcut">Ctrl / ⌘ + Enter</span>
        </div>
        <div className="query-form-footer">
          <span id="query-helper">
            <Icon name="lock" size={13} /> Searches stay in your workspace
          </span>
          <button
            className="primary-button query-submit"
            disabled={loading || !query.trim()}
            type="submit"
          >
            {loading ? (
              <span className="button-spinner" />
            ) : (
              <Icon name="sparkle" size={15} />
            )}
            {loading ? "Thinking" : "Ask context"}
          </button>
          <span className="sr-only" role="status">
            {loading
              ? "Searching your context"
              : answer.query
                ? "Context answer updated"
                : ""}
          </span>
        </div>
      </form>

      <div className="suggested-queries">
        <span>Try asking</span>
        <div>
          {suggestedQueries.map((suggestion) => (
            <button
              key={suggestion}
              onClick={() => onQueryChange(suggestion)}
              type="button"
            >
              {suggestion}
            </button>
          ))}
        </div>
      </div>

      <AnswerCard answer={answer} loading={loading} />
      <RankedResults results={results} />
    </section>
  );
}

function AnswerCard({
  answer,
  loading,
}: {
  answer: AnswerCard;
  loading: boolean;
}) {
  return (
    <article
      aria-busy={loading}
      aria-live="polite"
      className={`answer-card${loading ? " is-loading" : ""}`}
      id="context-answer"
      tabIndex={-1}
    >
      <div className="answer-card-head">
        <div className="answer-label">
          <span className="answer-spark">
            <Icon name="sparkle" size={14} />
          </span>{" "}
          Context answer
        </div>
        <span className={`confidence confidence-${answer.confidence}`}>
          <span /> {answer.confidence} confidence
        </span>
      </div>
      {loading ? (
        <div className="query-progress" role="status">
          <span className="button-spinner dark" />
          <div>
            <strong>Reviewing your recent activity</strong>
            <span>
              Gathering evidence and asking the local model. This usually
              takes 20–30 seconds.
            </span>
          </div>
        </div>
      ) : (
        <p className="answer-text">{answer.answer}</p>
      )}
      {answer.caveat ? (
        <div className="answer-caveat">
          <Icon name="info" size={14} /> {answer.caveat}
        </div>
      ) : null}
      <div className="evidence-heading">
        <span>Evidence</span>
        <span>{answer.evidence.length} sources</span>
      </div>
      <div className="evidence-list">
        {answer.evidence.map((item) => (
          <div className="evidence-item" key={item.id}>
            <div className={`source-avatar source-${item.source}`}>
              <SourceIcon source={item.source} size={15} />
            </div>
            <div className="evidence-copy">
              <strong>
                {item.url ? (
                  <a href={item.url} target="_blank" rel="noreferrer">
                    {item.title}
                  </a>
                ) : (
                  item.title
                )}
              </strong>
              <span>{item.detail}</span>
            </div>
            <div className="evidence-meta">
              <span
                className={`evidence-confidence confidence-${item.confidence}`}
                title={`${item.confidence} confidence`}
              >
                <span />
              </span>
              <time>{item.occurredAt}</time>
            </div>
          </div>
        ))}
      </div>
      <button className="evidence-link" type="button">
        View source trail <Icon name="arrowUpRight" size={14} />
      </button>
    </article>
  );
}

function RankedResults({ results }: { results: RankedResult[] }) {
  return (
    <section aria-labelledby="results-heading" className="ranked-results">
      <div className="ranked-heading">
        <div>
          <div className="section-kicker">
            <Icon name="filter" size={14} /> Ranked context
          </div>
          <h3 id="results-heading">Closest matches</h3>
        </div>
        <span className="result-count">{results.length} results</span>
      </div>
      <div className="result-list">
        {results.map((result) => (
          <article className="result-item" key={result.id}>
            <div className="rank-number">
              {String(result.rank).padStart(2, "0")}
            </div>
            <div className={`source-avatar source-${result.source}`}>
              <SourceIcon source={result.source} size={15} />
            </div>
            <div className="result-copy">
              <div className="result-title-row">
                <h4>{result.title}</h4>
                <span className="result-score">
                  {Math.round(result.score * 100)}%
                </span>
              </div>
              <p>
                {result.evidenceUrl ? (
                  <a href={result.evidenceUrl} target="_blank" rel="noreferrer">
                    {result.snippet}
                  </a>
                ) : (
                  result.snippet
                )}
              </p>
              <div className="result-meta">
                <span>{result.sourceLabel}</span>
                <span>·</span>
                <span>{result.occurredAt}</span>
                <span className="result-reason">{result.reason}</span>
              </div>
            </div>
            <Icon name="chevronRight" size={15} />
          </article>
        ))}
      </div>
      <button className="view-all-button" type="button">
        View all context <Icon name="arrowUpRight" size={14} />
      </button>
    </section>
  );
}
