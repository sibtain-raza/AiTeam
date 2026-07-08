import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";

export interface RunEvent {
  id: string;
  seq: number;
  at: string;
  source: string;
  event_type: string;
  detail: string;
  extra: string | null;
}

/** Subscribes to a run's live SSE event stream. Replays full history on
 * connect (the backend sends it before switching to live events), so this
 * works identically whether the run just started or finished an hour ago. */
export function useRunEvents(runId: string | undefined) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [ended, setEnded] = useState(false);
  const seenIds = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!runId) return;
    setEvents([]);
    setEnded(false);
    seenIds.current = new Set();

    const source = new EventSource(api.eventsUrl(runId));

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    source.onmessage = (evt) => {
      if (!evt.data) return;
      let payload: RunEvent | null = null;
      try {
        payload = JSON.parse(evt.data);
      } catch {
        return;
      }
      if (!payload || !payload.id || seenIds.current.has(payload.id)) return;
      seenIds.current.add(payload.id);
      setEvents((prev) => [...prev, payload as RunEvent].sort((a, b) => a.seq - b.seq));
    };

    source.addEventListener("end", () => {
      setEnded(true);
      source.close();
    });

    return () => source.close();
  }, [runId]);

  return { events, connected, ended };
}
