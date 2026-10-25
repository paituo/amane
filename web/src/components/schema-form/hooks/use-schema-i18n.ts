import { useTranslation } from "react-i18next";
import { devLog } from "@/lib/dev-logger";
import type { EnumSchema, JSONSchemaObject } from "../schema";

const TAG = "i18n";

/**
 * i18n lookup prefix: `"namespace:pathPrefix"` or just `"namespace"`.
 *
 * - namespace  = i18next resource namespace (e.g., "settings", "tasks")
 * - pathPrefix = dot-separated key prefix prepended to field paths (e.g., "fields")
 *
 * Examples:
 * - `"settings:fields"` → ns="settings", key = `fields.scraping.download_resources.label`
 * - `"tasks"`           → ns="tasks", key = `scrape.number.label` (no prefix)
 */

/** Split "namespace:pathPrefix" into its two parts. Empty or missing pathPrefix → "". */
function parsePrefix(prefix: string): { ns: string; keyPath: string } {
  const colon = prefix.indexOf(":");
  if (colon === -1) return { ns: prefix, keyPath: "" };
  return { ns: prefix.slice(0, colon), keyPath: prefix.slice(colon + 1) };
}

/** Build the full i18n key: optionally prepend keyPath, then append fieldPath and suffix. */
function buildKey(keyPath: string, fieldPath: string, suffix: string): string {
  if (keyPath) return `${keyPath}.${fieldPath}.${suffix}`;
  return `${fieldPath}.${suffix}`;
}

// ==================== Hooks ====================

/**
 * Three-tier i18n fallback for schema-driven form fields.
 * Priority: i18n translation > schema.title/description > raw field name
 *
 * Logs missing keys in dev mode.
 * 动态 path 无法进入 i18next 字面量 key 联合, 故 ns/key 使用 as never (动态表单例外).
 *
 * @param fieldPath  dot-separated path for i18n lookup (e.g., "scraping.download_resources")
 * @param schema     JSON Schema for the field
 * @param prefix     i18n prefix - "settings:fields" for config forms, "tasks" for task forms
 */
export function useSchemaI18n(
  fieldPath: string,
  schema: JSONSchemaObject,
  prefix: string,
): { label: string; description: string | undefined } {
  const { ns, keyPath } = parsePrefix(prefix);
  const { t, i18n } = useTranslation(ns as never);

  const label = (() => {
    const key = buildKey(keyPath, fieldPath, "label");
    if (i18n.exists(key, { ns: ns as never })) return t(key as never);
    devLog.warn(TAG, `Missing i18n key: ${key}`);
    return schema.title ?? fieldPath;
  })();

  const description = (() => {
    const key = buildKey(keyPath, fieldPath, "description");
    if (i18n.exists(key, { ns: ns as never })) return t(key as never);
    if (schema.description) {
      devLog.warn(TAG, `Missing i18n key: ${key}`);
    }
    return schema.description;
  })();

  return { label, description };
}

export function useEnumI18n(fieldPath: string, prefix: string) {
  const { ns, keyPath } = parsePrefix(prefix);
  const { t, i18n } = useTranslation(ns as never);

  return (value: string | number, index: number, schema: EnumSchema): string => {
    const key = buildKey(keyPath, fieldPath, `options.${value}`);
    if (i18n.exists(key, { ns: ns as never })) return t(key as never);
    const showNames = schema?.["x-show-names"];
    if (showNames?.[index]) return showNames[index];
    devLog.warn(TAG, `Missing i18n key: ${key}`);
    return String(value);
  };
}

/**
 * 逐项说明, 供 `RadioRows` 这类"一行一个选项"的版式使用.
 * 取值 `<fieldPath>.hints.<option>`; 没有该键时返回 undefined (视为这一项没有说明, 不是缺翻译).
 */
export function useEnumHintI18n(fieldPath: string, prefix: string) {
  const { ns, keyPath } = parsePrefix(prefix);
  const { t, i18n } = useTranslation(ns as never);

  return (value: string | number): string | undefined => {
    const key = buildKey(keyPath, fieldPath, `hints.${value}`);
    return i18n.exists(key, { ns: ns as never }) ? t(key as never) : undefined;
  };
}

/**
 * i18n for dict keys (e.g., field_language/field_priority tab labels).
 * Looks up `${keyPath}.${fieldPath}.options.${key}` for display name.
 */
export function useDictKeyI18n(fieldPath: string, prefix: string) {
  const { ns, keyPath } = parsePrefix(prefix);
  const { t, i18n } = useTranslation(ns as never);

  return (key: string): string => {
    const i18nKey = buildKey(keyPath, fieldPath, `options.${key}`);
    if (i18n.exists(i18nKey, { ns: ns as never })) return t(i18nKey as never);
    devLog.warn(TAG, `Missing i18n key: ${i18nKey}`);
    return key;
  };
}
