// A stand-in for the `chrome` extension API, just what the background worker uses, recording what it
// is asked to do. Events keep their listeners so a test can fire them.

class StubEvent {
  constructor() {
    this.listeners = [];
  }

  addListener(listener) {
    this.listeners.push(listener);
  }

  /** Calls every listener; returns what they returned. */
  fire(...args) {
    return this.listeners.map((listener) => listener(...args));
  }
}

/** cookies: what chrome.cookies.getAll answers, as a function of its filter (or an Error to throw). */
export function stubChrome({ cookies = () => [], alarms = {} } = {}) {
  const record = {
    menus: [],
    removedAllMenus: 0,
    alarmsCreated: [],
    alarmsCleared: [],
    notifications: [],
    cookieQueries: [],
    platformInfoCalls: 0,
  };
  const existingAlarms = new Map(Object.entries(alarms));
  const chrome = {
    record,
    existingAlarms, // the alarms the browser holds now, by name
    runtime: {
      id: "jpmdepemlhgipdkeancolccbfjeogjko",
      lastError: undefined,
      getURL: (path) => `chrome-extension://jpmdepemlhgipdkeancolccbfjeogjko/${path}`,
      getPlatformInfo: async () => {
        record.platformInfoCalls += 1;
        return { os: "win", arch: "x86-64", nacl_arch: "x86-64" };
      },
      onInstalled: new StubEvent(),
      onStartup: new StubEvent(),
      onMessage: new StubEvent(),
    },
    contextMenus: {
      onClicked: new StubEvent(),
      removeAll: async () => {
        record.removedAllMenus += 1;
        record.menus.length = 0;
      },
      create: (properties, callback) => {
        record.menus.push(properties);
        callback?.();
        return properties.id;
      },
    },
    alarms: {
      onAlarm: new StubEvent(),
      get: async (name) => existingAlarms.get(name),
      getAll: async () => [...existingAlarms.values()],
      clear: async (name) => {
        record.alarmsCleared.push(name);
        return existingAlarms.delete(name);
      },
      create: async (name, info) => {
        record.alarmsCreated.push({ name, ...info });
        existingAlarms.set(name, { name, ...info });
      },
    },
    notifications: {
      create: async (options) => {
        record.notifications.push(options);
        return `n${record.notifications.length}`;
      },
    },
    cookies: {
      getAll: async (filter) => {
        record.cookieQueries.push(filter);
        const answer = cookies(filter);
        if (answer instanceof Error) throw answer;
        return answer;
      },
    },
  };
  return chrome;
}
