// The live state behind the UI: every job, the queue, Snag's status and install progress, built from
// the hub's events by a pure reducer. Field names come from fields.js.

import {
  EVENT, CONNECTION, CONNECTION_STATES, jobFromEvent, queueFromEvent, statusFromEvent, removedIdFromEvent,
  progressFromEvent, settingsFromEvent, setupToolsFromStatus,
} from "./fields.js";

/**
 * connection: "connecting" | "connected" | "disconnected" (the hub's; both start "disconnected")
 * ready: a snapshot has arrived
 * jobs: {id: Job} with no prototype, so any id is just a key (state.jobs[id] reads any id safely)
 * queue: Queue | null, the one copy of the queue: from the snapshot, queue events, and status events
 *   that carry their own queue
 * status: Status merged from the snapshot and status events, without its queue
 * setup / updates: {tool: Progress}; setup also comes from a snapshot's or status event's own
 *   updater.setup.tools
 * settings: Settings | null, from the snapshot's settings and settings events (a page that loads them
 *   with GET /api/settings dispatches them as a settings event); a snapshot without any keeps them
 */
export function initialState() {
  return {
    connection: CONNECTION.DISCONNECTED, ready: false, jobs: Object.create(null), queue: null, status: {},
    setup: {}, updates: {}, settings: null,
  };
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function withoutQueue(status) {
  const { queue: _queue, ...rest } = status;
  return rest;
}

function copyJobs(jobs) {
  return Object.assign(Object.create(null), jobs);
}

/** The state after one event; the same object when the event changes nothing. Never mutates. */
export function reducer(state, event) {
  if (!isObject(event)) return state;
  switch (event.type) {
    case EVENT.SNAPSHOT: {
      const jobs = Object.create(null);
      for (const job of Array.isArray(event.jobs) ? event.jobs : []) {
        if (isObject(job) && typeof job.id === "string" && job.id) jobs[job.id] = job;
      }
      const status = isObject(event.status) ? event.status : {};
      const queue = isObject(event.queue) ? event.queue : isObject(status.queue) ? status.queue : null;
      const tools = setupToolsFromStatus(status);
      const settings = settingsFromEvent(event) ?? state.settings;
      return { ...state, ready: true, jobs, queue, status: withoutQueue(status), setup: tools ? { ...tools } : {}, settings };
    }
    case EVENT.JOB: {
      const job = jobFromEvent(event);
      if (!job) return state;
      const jobs = copyJobs(state.jobs);
      jobs[job.id] = job;
      return { ...state, jobs };
    }
    case EVENT.JOB_REMOVED: {
      const id = removedIdFromEvent(event);
      if (id === null || !Object.hasOwn(state.jobs, id)) return state;
      const jobs = copyJobs(state.jobs);
      delete jobs[id];
      return { ...state, jobs };
    }
    case EVENT.QUEUE: {
      const queue = queueFromEvent(event);
      return queue ? { ...state, queue } : state;
    }
    case EVENT.STATUS: {
      // Only this event's own fields count: older parts of the status never overwrite newer queue or
      // setup state that arrived in their own events.
      const update = statusFromEvent(event);
      if (Object.keys(update).length === 0) return state;
      const next = { ...state, status: { ...state.status, ...withoutQueue(update) } };
      if (isObject(update.queue)) next.queue = update.queue;
      const tools = setupToolsFromStatus(update);
      if (tools) next.setup = { ...tools };
      return next;
    }
    case EVENT.SETUP:
    case EVENT.UPDATE: {
      const { tool, progress } = progressFromEvent(event);
      if (!tool) return state;
      const key = event.type === EVENT.SETUP ? "setup" : "updates";
      return { ...state, [key]: { ...state[key], [tool]: progress } };
    }
    case EVENT.SETTINGS: {
      const settings = settingsFromEvent(event);
      return settings ? { ...state, settings } : state;
    }
    case EVENT.CONNECTION:
      if (!CONNECTION_STATES.includes(event.state) || event.state === state.connection) return state;
      return { ...state, connection: event.state };
    default:
      return state;
  }
}

function byCreated(a, b) {
  return (a.created_at ?? 0) - (b.created_at ?? 0) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

/** Every job ordered by created_at (oldest first, or newest first). */
export function jobList(state, { newestFirst = false } = {}) {
  const jobs = Object.values(state.jobs).sort(byCreated);
  return newestFirst ? jobs.reverse() : jobs;
}

/** {getState, dispatch(event), subscribe(fn(state, event)) → unsubscribe}. Subscribers hear only
 * events that changed the state. */
export function createStore(initial = initialState()) {
  let state = initial;
  const listeners = new Set();
  return {
    getState: () => state,
    dispatch(event) {
      const next = reducer(state, event);
      if (next === state) return;
      state = next;
      for (const listener of [...listeners]) {
        try {
          listener(state, event);
        } catch (error) {
          console.error("Snag: a store listener failed", error);
        }
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** Feed a hub's events and connection state into a store, starting with the hub's state now; returns
 * a function that stops it. */
export function bindHub(store, hub) {
  store.dispatch({ type: EVENT.CONNECTION, state: hub.connection });
  const offEvent = hub.onEvent((event) => store.dispatch(event));
  const offConnection = hub.onConnection((connection) => store.dispatch({ type: EVENT.CONNECTION, state: connection }));
  return () => {
    offEvent();
    offConnection();
  };
}
