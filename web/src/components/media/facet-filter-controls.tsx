/**
 * 片库列表的高级 facet 筛选: 展开后每个 kind 独立多选框, 一次可勾多个, 勾完即写回 URL search.
 * 另含媒体库、条目类型与关联文件相位筛选.
 */

import { Collapse, Group, MultiSelect, Select, SimpleGrid, Stack, Text } from "@mantine/core";
import { IconCheck } from "@tabler/icons-react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getFacetOptions,
  listFacetsOptions,
  listLibrariesOptions,
} from "@/client/@tanstack/react-query.gen";
import type { ContentType, FacetKind, Mosaic } from "@/client/types.gen";
import { CONTENT_TYPES, FACET_KINDS, FILE_DEFINITIONS, MOSAICS } from "@/lib/exhaustive-maps";
import { facetIdsOf, type FacetFilters } from "@/lib/facets";

const PICKER_LIMIT = 40;

/** URL / 控件用的三态; null = 不限. */
export type HasFilesFilter = boolean | null;

export type FilePhaseFilters = {
  has_subtitle: HasFilesFilter;
  uncensored: HasFilesFilter;
  /** 多选: 同一类多值是 OR (任一命中即可); 空数组 = 不限. */
  mosaic: Mosaic[];
  definition: (typeof FILE_DEFINITIONS)[number][];
  content_type: ContentType[];
};

interface FacetFilterControlsProps {
  /** 是否展开高级筛选表单. */
  opened: boolean;
  /** 当前已激活的过滤参数; 多选控件直接回填已选的 id. */
  filters: FacetFilters;
  /** 某个 kind 的选中项整组变化 (空数组 = 取消该 kind 的筛选). */
  onChangeFacet: (kind: FacetKind, ids: number[]) => void;
  /** 媒体库筛选; 空数组 = 不限. */
  libraryIds: readonly number[];
  onLibraryIdsChange: (ids: number[]) => void;
  /** 当前关联文件筛选; null = 不限. */
  hasFiles: HasFilesFilter;
  onHasFilesChange: (value: HasFilesFilter) => void;
  /** 片库级 VR 判定 (来自标签), 与文件相位无关. */
  vr: HasFilesFilter;
  onVrChange: (value: HasFilesFilter) => void;
  filePhase: FilePhaseFilters;
  onFilePhaseChange: (value: FilePhaseFilters) => void;
}

/** 库全部由 `GET /libraries` 提供, 没有分页; 多选命中任一库的影片即算命中. */
function LibraryPicker({
  libraryIds,
  onChange,
  enabled,
}: {
  libraryIds: readonly number[];
  onChange: (ids: number[]) => void;
  enabled: boolean;
}) {
  const { t } = useTranslation("metadata");
  const { data, isFetching } = useQuery({ ...listLibrariesOptions(), enabled });

  return (
    <MultiSelect
      label={t("search.library")}
      placeholder={t("search.any")}
      data={(data?.items ?? []).map((library) => ({
        value: String(library.id),
        label: library.name,
      }))}
      value={libraryIds.map(String)}
      onChange={(values) => {
        const ids = values.map(Number).filter((id) => Number.isInteger(id) && id > 0);
        onChange(ids);
      }}
      nothingFoundMessage={isFetching ? "…" : t("search.facetEmpty")}
      clearable
      searchable
      size="sm"
    />
  );
}

function KindFacetPicker({
  kind,
  filters,
  onChangeFacet,
  enabled,
}: {
  kind: FacetKind;
  filters: FacetFilters;
  onChangeFacet: (kind: FacetKind, ids: number[]) => void;
  enabled: boolean;
}) {
  const { t } = useTranslation("metadata");
  const [search, setSearch] = useState("");
  // 浮层保持默认 portal, 不因"面板在抽屉里"而改成 `withinPortal: false`:
  // 实测浮层挂到 body 上不会关掉抽屉 (依据见 docs/dev/frontend.md).

  const { data, isFetching } = useQuery({
    ...listFacetsOptions({
      path: { kind },
      query: { search: search || undefined, limit: PICKER_LIMIT, offset: 0 },
    }),
    enabled,
  });

  const selectedIds = facetIdsOf(filters, kind);
  const items = data?.items ?? [];
  // label 只放名称: MultiSelect 的 chip 直接取 option.label, 带上计数就会写成 "4K (2060)" 这种噪音.
  // 计数改由 renderOption 在下拉里单独摆.
  const counts = new Map(items.map((facet) => [String(facet.id), facet.count]));
  const options = items.map((facet) => ({
    value: String(facet.id),
    label: facet.name,
  }));

  // 已选但不在当前结果集里的项也要显示, 否则搜索框一改, 选中项看起来就丢了.
  // 补占位项而不是只塞进 value: 不在 data 里的 value 显示不出来, 看起来就"被取消了".
  // 占位项的名称单独按 id 拉 (一次一个, 只对挤出去的已选项发请求), 否则 chip 上只剩 "7" 这种 id.
  const known = new Set(options.map((option) => option.value));
  const missingIds = selectedIds.filter((id) => !known.has(String(id)));
  const missingNames = useQueries({
    queries: missingIds.map((id) => ({
      ...getFacetOptions({ path: { kind, facet_id: id } }),
      staleTime: Infinity,
    })),
  });
  const pinned = missingIds.map((id, index) => ({
    value: String(id),
    label: missingNames[index]?.data?.name ?? `#${id}`,
  }));
  const value = selectedIds.map(String);

  return (
    <MultiSelect
      label={t(`browse.kinds.${kind}`)}
      placeholder={t("search.facetPlaceholder")}
      data={[...options, ...pinned]}
      value={value}
      searchable
      searchValue={search}
      onSearchChange={setSearch}
      nothingFoundMessage={isFetching ? "…" : t("search.facetEmpty")}
      onChange={(values) => {
        const ids = values.map(Number).filter((id) => Number.isInteger(id) && id > 0);
        onChangeFacet(kind, ids);
      }}
      hidePickedOptions={false}
      // 换掉默认内容就丢了勾选图标, 自己补一个; 顺手把计数摆在右侧.
      renderOption={({ option, checked }) => {
        const count = counts.get(option.value);
        return (
          <Group justify="space-between" w="100%" wrap="nowrap" gap="xs">
            <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
              <IconCheck size={12} style={{ visibility: checked ? "visible" : "hidden" }} />
              <Text size="sm" truncate="end">
                {option.label}
              </Text>
            </Group>
            {count === undefined ? null : (
              <Text size="xs" c="dimmed">
                {count}
              </Text>
            )}
          </Group>
        );
      }}
      clearable
      size="sm"
    />
  );
}

function triSelectValue(value: HasFilesFilter): string | null {
  if (value === true) return "true";
  if (value === false) return "false";
  return null;
}

function parseTriSelect(value: string | null): HasFilesFilter {
  if (value === "true") return true;
  if (value === "false") return false;
  return null;
}

export function FacetFilterControls({
  opened,
  filters,
  onChangeFacet,
  libraryIds,
  onLibraryIdsChange,
  hasFiles,
  onHasFilesChange,
  vr,
  onVrChange,
  filePhase,
  onFilePhaseChange,
}: FacetFilterControlsProps) {
  const { t } = useTranslation("metadata");
  const triData = [
    { value: "true", label: t("search.yes") },
    { value: "false", label: t("search.no") },
  ];

  return (
    <Collapse expanded={opened}>
      <Stack gap="xs">
        <Text size="sm" c="dimmed">
          {t("search.advancedHint")}
        </Text>
        <SimpleGrid cols={{ base: 1, xs: 2, sm: 3, md: 4 }} spacing="sm">
          {FACET_KINDS.map((kind) => (
            <KindFacetPicker
              key={kind}
              kind={kind}
              filters={filters}
              onChangeFacet={onChangeFacet}
              enabled={opened}
            />
          ))}
          <LibraryPicker libraryIds={libraryIds} onChange={onLibraryIdsChange} enabled={opened} />
          <Select
            label={t("search.hasFiles")}
            placeholder={t("search.hasFilesAny")}
            data={[
              { value: "true", label: t("search.hasFilesYes") },
              { value: "false", label: t("search.hasFilesNo") },
            ]}
            value={triSelectValue(hasFiles)}
            onChange={(v) => onHasFilesChange(parseTriSelect(v))}
            clearable
            size="sm"
          />
          <Select
            label={t("search.vr")}
            placeholder={t("search.any")}
            data={triData}
            value={triSelectValue(vr)}
            onChange={(v) => onVrChange(parseTriSelect(v))}
            clearable
            size="sm"
          />
          <Select
            label={t("search.hasSubtitle")}
            placeholder={t("search.any")}
            data={triData}
            value={triSelectValue(filePhase.has_subtitle)}
            onChange={(v) => onFilePhaseChange({ ...filePhase, has_subtitle: parseTriSelect(v) })}
            clearable
            size="sm"
          />
          <Select
            label={t("search.uncensored")}
            placeholder={t("search.any")}
            data={triData}
            value={triSelectValue(filePhase.uncensored)}
            onChange={(v) => onFilePhaseChange({ ...filePhase, uncensored: parseTriSelect(v) })}
            clearable
            size="sm"
          />
          {/* 三者都是同一类多值: 多选, 服务端按 OR 求并集. 布尔类 (有文件 / VR / 中字 / 无码)
              保持单选 —— 是/否两个都勾等于不限, 多选在那里没有语义. */}
          <MultiSelect
            label={t("search.mosaic")}
            placeholder={t("search.any")}
            data={MOSAICS.map((mosaic) => ({
              value: mosaic,
              label: t(`search.mosaics.${mosaic}`),
            }))}
            value={filePhase.mosaic}
            onChange={(values) =>
              onFilePhaseChange({ ...filePhase, mosaic: MOSAICS.filter((v) => values.includes(v)) })
            }
            clearable
            size="sm"
          />
          <MultiSelect
            label={t("search.definition")}
            placeholder={t("search.any")}
            data={FILE_DEFINITIONS.map((definition) => ({
              value: definition,
              label: definition,
            }))}
            value={filePhase.definition}
            onChange={(values) =>
              onFilePhaseChange({
                ...filePhase,
                definition: FILE_DEFINITIONS.filter((v) => values.includes(v)),
              })
            }
            clearable
            size="sm"
          />
          <MultiSelect
            label={t("search.contentType")}
            placeholder={t("search.any")}
            data={CONTENT_TYPES.map((contentType) => ({
              value: contentType,
              label: t(`search.contentTypes.${contentType}`),
            }))}
            value={filePhase.content_type}
            onChange={(values) =>
              onFilePhaseChange({
                ...filePhase,
                content_type: CONTENT_TYPES.filter((v) => values.includes(v)),
              })
            }
            clearable
            size="sm"
          />
        </SimpleGrid>
      </Stack>
    </Collapse>
  );
}
