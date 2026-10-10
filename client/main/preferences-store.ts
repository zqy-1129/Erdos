import type { SecretStore } from "./key-vault.ts";
import { DEFAULT_PREFERENCES, parsePreferences, type ClientPreferences } from "../shared/preferences.ts";

/** 偏好持久化端口：复用本地 SQLite，写成功后才向界面返回成功。 */
export class PreferencesStore {
  private readonly store: SecretStore;
  constructor(store: SecretStore) { this.store = store; }
  load(): ClientPreferences {
    const raw = this.store.get("client.preferences.v1");
    if (!raw) return { ...DEFAULT_PREFERENCES };
    try { return parsePreferences(JSON.parse(raw)); }
    catch { throw new Error("本地偏好配置损坏，请重新保存设置"); }
  }
  save(value: unknown): ClientPreferences {
    const parsed = parsePreferences(value);
    this.store.set("client.preferences.v1", JSON.stringify(parsed));
    return parsed;
  }
}
