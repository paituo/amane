/**
 * 与后端 MetadataListParams 对齐 (snake_case).
 * 仅 UI 导航态保留 q / view / page / saved_query_id.
 *
 * 与 `lib/actors/browse.ts` 同形: schema 放在 lib 而不是路由里, 侧栏默认参数的白名单
 * (见 `lib/nav-defaults.ts`) 才能引用它 —— 从侧栏组件静态导入路由模块会把该路由的 chunk
 * 并入入口 chunk.
 */

import type { ParseKeys } from "i18next";
import { z } from "zod";
import type { MetadataSortField, SearchField } from "@/client/types.gen";
import {
  CONTENT_TYPES,
  FILE_DEFINITIONS,
  METADATA_SORT_FIELDS,
  MOSAICS,
  SEARCH_FIELDS,
  SEARCH_TYPES,
  SORT_ORDERS,
} from "@/lib/exhaustive-maps";
import { coerceIdList } from "@/lib/facets";

const idListSchema = z.preprocess(coerceIdList, z.array(z.number().int().positive()).optional());

/**
 * 检索方式的缺省值.
 *
 * 与后端不同: 后端 `search_type` 缺省表示"跟随配置", 而前端必须给出一个确定的选择,
 * 否则菜单里没有选中项. 因此这个默认值由前端在每次请求里显式带上.
 */
export const DEFAULT_SEARCH_TYPE = "hybrid" as const satisfies (typeof SEARCH_TYPES)[number];

/** 全选等价于不限; 此时不写进 URL, 后端缺参即全选, 地址也不会随字段个数膨胀. */
export function isAllSearchFields(fields: readonly SearchField[]): boolean {
  return fields.length >= SEARCH_FIELDS.length;
}

function coerceSearchFields(value: unknown): SearchField[] | undefined {
  if (value == null || value === "") return undefined;
  const raw = Array.isArray(value) ? value : [value];
  const fields = raw.filter((item): item is SearchField =>
    SEARCH_FIELDS.includes(item as SearchField),
  );
  return fields.length > 0 ? fields : undefined;
}

const searchFieldListSchema = z.preprocess(
  coerceSearchFields,
  z.array(z.enum(SEARCH_FIELDS)).optional(),
);

/**
 * 字符串枚举多值: 单值 (旧链接 / 保存的预设) 与数组都能收, 非法值与重复项丢弃.
 * 空结果返回 undefined —— 不限不写进 URL, 地址才不会随默认值膨胀.
 */
function coerceEnumList<T extends string>(value: unknown, allowed: readonly T[]): T[] | undefined {
  if (value == null || value === "") return undefined;
  const permitted = new Set<string>(allowed);
  const raw = Array.isArray(value) ? value : [value];
  const picked: T[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (typeof item !== "string" || seen.has(item) || !permitted.has(item)) continue;
    seen.add(item);
    picked.push(item as T); // Set<string> 已在上一行校验过取值域, 收窄到枚举成员是安全的.
  }
  return picked.length > 0 ? picked : undefined;
}

/** 同一类多值在服务端是 OR (任一命中即可), 见 docs/dev/api.md. */
function enumListSchema<T extends string>(allowed: readonly T[]) {
  return z.preprocess(
    (value) => coerceEnumList(value, allowed),
    z.array(z.enum(allowed)).optional(),
  );
}

/** 片库与演员详情出演作品共用的排序项; 数组顺序即菜单显示顺序. */
export const METADATA_SORT_OPTIONS = [
  { value: "updated_at", labelKey: "columns.updated" },
  { value: "created_at", labelKey: "columns.created" },
  { value: "number", labelKey: "columns.number" },
  { value: "title", labelKey: "columns.title" },
  { value: "studio", labelKey: "columns.studio" },
  { value: "release", labelKey: "columns.release" },
  { value: "score", labelKey: "columns.score" },
  { value: "file_count", labelKey: "columns.fileCount" },
] as const satisfies readonly {
  value: MetadataSortField;
  labelKey: ParseKeys<"metadata">;
}[];

/** 排序记忆值: 演员详情页的作品排序存于 ui store, 读取时经此校验, 非法整值丢弃. */
export const metadataSortPreferenceSchema = z.object({
  sort_by: z.enum(METADATA_SORT_FIELDS),
  order: z.enum(SORT_ORDERS),
});

export type MetadataSortPreference = z.infer<typeof metadataSortPreferenceSchema>;

/** 与后端列表默认一致: 无记忆时出演作品的顺序不变. */
export const DEFAULT_METADATA_SORT_PREFERENCE: MetadataSortPreference = {
  sort_by: "updated_at",
  order: "desc",
};

export const metaSearchSchema = z.object({
  q: z.string().optional(),
  // 缺省不写进 URL: 读取处按 DEFAULT_SEARCH_TYPE 兜底, 地址栏才不会被默认值塞满.
  // 非法值 (手改过的链接) 一并退回 undefined, 走同一个兜底.
  search_type: z.enum(SEARCH_TYPES).optional().catch(undefined),
  search_fields: searchFieldListSchema,
  view: z.enum(["grid", "list"]).catch("grid").default("grid"),
  sort_by: z.enum(METADATA_SORT_FIELDS).optional(),
  order: z.enum(SORT_ORDERS).optional(),
  page: z.coerce.number().int().min(1).catch(1).default(1),
  actor_id: idListSchema,
  director_id: idListSchema,
  tag_id: idListSchema,
  studio_id: idListSchema,
  publisher_id: idListSchema,
  series_id: idListSchema,
  user_tag_id: idListSchema,
  library_id: idListSchema,
  has_files: z.enum(["true", "false"]).optional(),
  has_subtitle: z.enum(["true", "false"]).optional(),
  vr: z.enum(["true", "false"]).optional(),
  uncensored: z.enum(["true", "false"]).optional(),
  mosaic: enumListSchema(MOSAICS),
  definition: enumListSchema(FILE_DEFINITIONS),
  content_type: enumListSchema(CONTENT_TYPES),
  saved_query_id: z.coerce.number().int().positive().optional(),
});

export type MetaSearch = z.infer<typeof metaSearchSchema>;
