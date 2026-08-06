import type {
  AnswerCard,
  DashboardSnapshot,
  SearchParams,
  SearchResponse,
  SourceKind,
} from "./types";

const DEMO_NOW = "2026-08-05T16:32:00-04:00";

const sourceLabels: Record<SourceKind, string> = {
  messages: "Messages",
  documents: "Documents",
  calendar: "Calendar",
  tasks: "Tasks",
};

const sourceDescriptions: Record<SourceKind, string> = {
  messages: "Private conversations and saved threads",
  documents: "Notes, briefs, and working documents",
  calendar: "Meetings, focus blocks, and commitments",
  tasks: "Open loops from your task manager",
};

export function buildDemoSnapshot(): DashboardSnapshot {
  return {
    session: {
      id: "session_demo_2026_08_05",
      title: "Wednesday workspace",
      state: "live",
      startedAt: "2026-08-05T08:04:00-04:00",
      lastEventAt: DEMO_NOW,
      eventCount: 1284,
      participantCount: 3,
      privacyMode: "private",
      transport: "connected",
    },
    timeline: [
      {
        id: "timeline_1",
        occurredAt: "2026-08-05T16:24:00-04:00",
        relativeLabel: "8 min ago",
        source: "messages",
        sourceLabel: sourceLabels.messages,
        title: "Maya shared the revised launch brief",
        summary:
          "The positioning section now leads with the faster setup time and moves the migration story into proof points.",
        tags: ["launch", "positioning"],
        participant: "Maya Chen",
        important: true,
      },
      {
        id: "timeline_2",
        occurredAt: "2026-08-05T15:52:00-04:00",
        relativeLabel: "40 min ago",
        source: "calendar",
        sourceLabel: sourceLabels.calendar,
        title: "Product review moved to Thursday",
        summary:
          "The review is now Thursday at 10:30 AM. The agenda still includes onboarding friction and the Q3 activation readout.",
        tags: ["schedule", "product"],
        participant: "Product team",
      },
      {
        id: "timeline_3",
        occurredAt: "2026-08-05T14:18:00-04:00",
        relativeLabel: "2 hr ago",
        source: "documents",
        sourceLabel: sourceLabels.documents,
        title: "Research synthesis added to the workspace",
        summary:
          "Eight customer interviews point to a recurring need for visible progress and a smaller first step.",
        tags: ["research", "insight"],
        participant: "Research synthesis",
        important: true,
      },
      {
        id: "timeline_4",
        occurredAt: "2026-08-05T12:46:00-04:00",
        relativeLabel: "4 hr ago",
        source: "tasks",
        sourceLabel: sourceLabels.tasks,
        title: "Follow up with design on the empty state",
        summary:
          "The task is blocked on a decision about whether the first-run state should explain the system or invite an action.",
        tags: ["design", "blocked"],
        participant: "You",
      },
      {
        id: "timeline_5",
        occurredAt: "2026-08-05T10:10:00-04:00",
        relativeLabel: "6 hr ago",
        source: "messages",
        sourceLabel: sourceLabels.messages,
        title: "Jordan confirmed the analytics naming pass",
        summary:
          "The new event names are ready for implementation, with one open question around reactivation cohorts.",
        tags: ["analytics", "implementation"],
        participant: "Jordan Patel",
      },
      {
        id: "timeline_6",
        occurredAt: "2026-08-04T17:36:00-04:00",
        relativeLabel: "Yesterday",
        source: "documents",
        sourceLabel: sourceLabels.documents,
        title: "Activation notes became the working narrative",
        summary:
          "The team agreed to measure the first meaningful outcome rather than raw setup completion.",
        tags: ["activation", "strategy"],
        participant: "Working notes",
      },
    ],
    timelineTotalCount: 6,
    answer: buildDemoAnswer("What changed around the launch this week?"),
    rankedResults: buildDemoResults(),
    queue: {
      state: "stale",
      lastIndexedAt: "2026-08-05T16:27:00-04:00",
      lagSeconds: 302,
      pending: 17,
      failed: 1,
      deadLettered: 0,
      message:
        "New events are still searchable, but the latest five minutes may arrive out of order.",
    },
    settings: {
      routes: [
        ...(Object.keys(sourceLabels) as SourceKind[]).map((source) => ({
          source,
          label: sourceLabels[source],
          enabled: true,
          route: source === "messages" ? "local" : "cloud",
          description: sourceDescriptions[source],
          lastSyncedAt: source === "messages" ? "Just now" : "4 min ago",
        })),
        {
          source: "clipboard",
          label: "Clipboard",
          enabled: true,
          route: "local",
          description: "Raw copied text stays local by default",
          lastSyncedAt: "Just now",
        },
        {
          source: "screenshots",
          label: "Screenshots",
          enabled: true,
          route: "remote",
          description: "Captured images and evidence artifacts",
          lastSyncedAt: "4 min ago",
        },
      ],
      retention: {
        eventsDays: 365,
        transcriptsDays: 30,
        rawPayloadDays: 7,
        deleteAfterExport: false,
      },
      plaintextWarningAcknowledged: false,
      privacyWarning:
        "Raw clipboard text and screenshots are stored as plaintext in the configured archive. Clipboard is local-only by default; configure remote routing explicitly.",
      captureIntervalSeconds: 5,
      workerCount: 1,
      queueSize: 1000,
      modelEndpoint: "",
      modelName: "",
      modelRevision: "",
    },
    runtime: {
      workerCount: 1,
      queueSize: 1000,
      model: {
        configured: false,
        status: "not_configured",
        workerStatus: "not_configured",
        modelStatus: "not_configured",
      },
    },
  };
}

function buildDemoAnswer(query: string): AnswerCard {
  return {
    query,
    answer:
      "The launch narrative tightened around a faster first-run experience. Maya’s revised brief makes setup time the lead, while interview notes reinforce that visible progress matters more than explaining every capability up front.",
    confidence: "high",
    generatedAt: DEMO_NOW,
    caveat:
      "This answer combines messages and documents indexed through 4:27 PM.",
    evidence: [
      {
        id: "evidence_1",
        source: "messages",
        sourceLabel: sourceLabels.messages,
        title: "Revised launch brief",
        detail:
          "Setup time moved into the lead position of the launch narrative.",
        occurredAt: "8 min ago",
        confidence: "high",
      },
      {
        id: "evidence_2",
        source: "documents",
        sourceLabel: sourceLabels.documents,
        title: "Customer interview synthesis",
        detail:
          "Eight interviews favor a visible first step and clear progress cues.",
        occurredAt: "2 hr ago",
        confidence: "high",
      },
      {
        id: "evidence_3",
        source: "calendar",
        sourceLabel: sourceLabels.calendar,
        title: "Product review agenda",
        detail:
          "Thursday’s review includes onboarding friction as a central discussion.",
        occurredAt: "40 min ago",
        confidence: "medium",
      },
    ],
  };
}

function buildDemoResults() {
  return [
    {
      id: "result_1",
      rank: 1,
      title: "Revised launch brief",
      snippet:
        "Lead with faster setup time; keep migration proof points as supporting evidence.",
      source: "messages" as const,
      sourceLabel: sourceLabels.messages,
      occurredAt: "8 min ago",
      score: 0.94,
      reason: "Matches launch + positioning",
      tags: ["launch", "positioning"],
    },
    {
      id: "result_2",
      rank: 2,
      title: "Customer interview synthesis",
      snippet:
        "People need a visible first step and progress they can understand without a tutorial.",
      source: "documents" as const,
      sourceLabel: sourceLabels.documents,
      occurredAt: "2 hr ago",
      score: 0.89,
      reason: "Supports the same narrative",
      tags: ["research", "activation"],
    },
    {
      id: "result_3",
      rank: 3,
      title: "Product review agenda",
      snippet:
        "Onboarding friction remains on the agenda for Thursday’s review.",
      source: "calendar" as const,
      sourceLabel: sourceLabels.calendar,
      occurredAt: "40 min ago",
      score: 0.78,
      reason: "Recent related commitment",
      tags: ["product", "schedule"],
    },
    {
      id: "result_4",
      rank: 4,
      title: "Empty state decision",
      snippet:
        "The open question is whether the first-run screen teaches or invites an action.",
      source: "tasks" as const,
      sourceLabel: sourceLabels.tasks,
      occurredAt: "4 hr ago",
      score: 0.65,
      reason: "Related open loop",
      tags: ["design", "blocked"],
    },
  ];
}

export async function searchDemoSnapshot(
  snapshot: DashboardSnapshot,
  params: SearchParams,
): Promise<SearchResponse> {
  const query = params.query.trim().toLowerCase();
  if (!query) {
    return { answer: snapshot.answer, rankedResults: snapshot.rankedResults };
  }

  const queryWords = query
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .split(/\s+/)
    .filter(Boolean);
  const rankedResults = snapshot.rankedResults
    .map((result) => {
      const haystack =
        `${result.title} ${result.snippet} ${result.tags.join(" ")}`.toLowerCase();
      const matches = queryWords.filter((word) =>
        haystack.includes(word),
      ).length;
      return { result, matches };
    })
    .filter(({ matches }) => matches > 0)
    .sort((a, b) => b.matches - a.matches || b.result.score - a.result.score)
    .map(({ result }, index) => ({ ...result, rank: index + 1 }));

  const visibleResults =
    rankedResults.length > 0
      ? rankedResults
      : snapshot.rankedResults.slice(0, 3);
  const answer: AnswerCard = {
    ...snapshot.answer,
    query: params.query,
    answer: rankedResults.length
      ? `The strongest context for “${params.query}” is ${visibleResults
          .slice(0, 2)
          .map((result) => result.title)
          .join(
            " and ",
          )}. Together they point to a clear, recent thread rather than a single isolated event.`
      : `I could not find an exact match for “${params.query}”, but these nearby items may help orient the search.`,
    confidence: rankedResults.length ? "medium" : "low",
    generatedAt: DEMO_NOW,
    caveat: rankedResults.length
      ? "Demo search ranks local sample context. A configured API can replace this response."
      : "Try a person, project, decision, or time range.",
    evidence: visibleResults.slice(0, 3).map((result) => ({
      id: `evidence_${result.id}`,
      source: result.source,
      sourceLabel: result.sourceLabel,
      title: result.title,
      detail: result.snippet,
      occurredAt: result.occurredAt,
      confidence: result.score > 0.85 ? "high" : "medium",
    })),
  };

  return { answer, rankedResults: visibleResults };
}
