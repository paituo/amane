import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Center,
  Checkbox,
  Group,
  Menu,
  Stack,
  Table,
  Text,
  type MantineBreakpoint,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconDots,
  IconFolderDown,
  IconForms,
  IconPhoto,
  IconPlaylistAdd,
  IconPlayerPlay,
  IconPlayerPlayFilled,
  IconRefresh,
  IconSelectAll,
  IconSquareOff,
  IconTrash,
} from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { listMediaQueryKey, openLocalPlaylistMutation } from "@/client/@tanstack/react-query.gen";
import { batchDeleteMedia, batchSubmitTasks, deleteMedia, submitTask } from "@/client/sdk.gen";
import type {
  ContentType,
  MediaFileResponse,
  MediaFileStatus,
  MediaSortField,
  SortOrder,
} from "@/client/types.gen";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { FilePhaseBadges } from "@/components/media/file-phase-badges";
import { ListToolbar } from "@/components/common/list-toolbar";
import { PlaybackModal } from "@/components/media/playback-modal";
import { PlaybackQueueModal } from "@/components/media/playback-queue-modal";
import { ProxyImage } from "@/components/media/proxy-image";
import { SortableTh } from "@/components/common/sortable-th";
import { SelectionBar } from "@/components/common/selection-bar";
import { ScrapeOverrideDialog } from "./scrape-override-dialog";
import { MediaDeleteDialog } from "./media-delete-dialog";
import { useAppMutation } from "@/hooks/use-app-mutation";
import { useIdSelection } from "@/hooks/use-id-selection";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { assertNever } from "@/lib/exhaustive";
import { useLocalPlayer, useOpenLocalPlayback } from "@/lib/media/local-player";
import { entryFromMediaFile } from "@/lib/media/playlist-entries";
import { type PlaylistEntry, usePlaylistStore } from "@/stores/playlist";
import { formatFileSize, proxyImageUrl } from "@/lib/utils";
import { useUIStore } from "@/stores/ui";
import classes from "./library-media-table.module.css";

function statusColor(status: MediaFileStatus): string {
  switch (status) {
    case "scraped":
      return "teal";
    case "failed":
      return "red";
    case "skip":
      return "gray";
    case "pending":
      return "yellow";
    default:
      return assertNever(status, "MediaFileStatus");
  }
}

/** library.path 前缀去除, 得到库内相对路径; 非该库路径时原样返回. */
function relativePath(libraryPath: string, path: string): string {
  if (path.startsWith(libraryPath)) {
    return path.slice(libraryPath.length).replace(/^\//, "");
  }
  return path;
}

/** 行内缩略海报: 与海报墙同 0.7 比例, 高度由宽度算得 (44 / 0.7 ≈ 63). */
const POSTER_WIDTH = 44;
const POSTER_HEIGHT = 63;

interface RowPosterProps {
  item: MediaFileResponse;
  onPlay: (item: MediaFileResponse) => void;
}

/**
 * 列表行的封面预览与播放入口.
 *
 * 封面来自所属条目的首个 `poster_url`; 未绑定条目时只有占位块, 也没有播放按钮 —
 * 播放端点按条目取流, 没有条目就没有可播的对象.
 */
function RowPoster({ item, onPlay }: RowPosterProps) {
  const { t } = useTranslation("library");
  const [errored, setErrored] = useState(false);
  const src = proxyImageUrl(item.poster_url);
  const metadataId = item.metadata_id;
  const frameStyle = {
    overflow: "hidden",
    borderRadius: "var(--mantine-radius-sm)",
    background: "var(--mantine-color-default-hover)",
  } as const;

  return (
    <Box pos="relative" w={POSTER_WIDTH} h={POSTER_HEIGHT}>
      {metadataId != null ? (
        <Link
          to="/meta/$metadataId"
          params={{ metadataId: String(metadataId) }}
          style={{ display: "block", height: "100%" }}
        >
          <Box w="100%" h="100%" style={frameStyle}>
            {src && !errored ? (
              <ProxyImage
                src={src}
                alt={item.number ?? ""}
                loading="lazy"
                referrerPolicy="no-referrer"
                style={{ display: "block", width: "100%", height: "100%", objectFit: "cover" }}
                onError={() => setErrored(true)}
              />
            ) : (
              <Center h="100%">
                <IconPhoto size={16} color="var(--mantine-color-dimmed)" />
              </Center>
            )}
          </Box>
        </Link>
      ) : (
        <Center h="100%" style={frameStyle}>
          <IconPhoto size={16} color="var(--mantine-color-dimmed)" />
        </Center>
      )}
      {metadataId != null && (
        <HintedActionIcon
          size={20}
          radius="xl"
          variant="transparent"
          color="white"
          style={{ position: "absolute", right: 2, bottom: 2, background: "rgba(0, 0, 0, 0.55)" }}
          label={t("actions.play")}
          onClick={() => onPlay(item)}
        >
          <IconPlayerPlayFilled size={11} />
        </HintedActionIcon>
      )}
    </Box>
  );
}

interface DeleteMenuItemsProps {
  onDeleteRecord: () => void;
  onDeleteFiles: () => void;
  onDeleteWorkDir: () => void;
  disabled?: boolean;
}

/** 三个删除口径在批量与单行菜单里完全一致, 只在单行菜单里没有勾选量. */
function DeleteMenuItems({
  onDeleteRecord,
  onDeleteFiles,
  onDeleteWorkDir,
  disabled,
}: DeleteMenuItemsProps) {
  const { t } = useTranslation("library");
  return (
    <>
      <Menu.Item disabled={disabled} onClick={onDeleteRecord}>
        {t("cleanup.deleteRecord")}
      </Menu.Item>
      <Menu.Item disabled={disabled} onClick={onDeleteFiles}>
        {t("cleanup.deleteFiles")}
      </Menu.Item>
      <Menu.Item disabled={disabled} onClick={onDeleteWorkDir}>
        {t("cleanup.deleteWorkDir")}
      </Menu.Item>
    </>
  );
}

const SORTABLE_COLUMNS = [
  "path",
  "status",
  "size",
  "updated_at",
] as const satisfies readonly MediaSortField[];

type SortableColumn = (typeof SORTABLE_COLUMNS)[number];

const COLUMN_I18N_KEY = {
  path: "path",
  status: "status",
  size: "size",
  updated_at: "updated",
} as const satisfies Record<SortableColumn, string>;

const COLUMN_WIDTH: Record<SortableColumn, number | undefined> = {
  path: undefined,
  status: 110,
  size: 90,
  updated_at: 110,
};

const CELL_OVERFLOW = { overflow: "hidden", maxWidth: 0 } as const;

/**
 * 各列的隐藏断点: 可见列宽之和超出容器时表格横向滚动, 路径列随之移出首屏.
 * 该映射同时决定表头与单元格的显隐, 任一处缺失即列错位.
 */
const COLUMN_VISIBLE_FROM: Partial<Record<SortableColumn, MantineBreakpoint>> = {
  size: "sm",
  updated_at: "sm",
};

/** 元数据列不可排序, 与 size / updated 同在 sm 以下隐藏. */
const METADATA_VISIBLE_FROM: MantineBreakpoint = "sm";

export interface LibraryMediaTableProps {
  libraryId: number;
  libraryPath: string;
  items: MediaFileResponse[];
  isLoading: boolean;
  total: number;
  page: number;
  sortBy: MediaSortField | undefined;
  order: SortOrder | undefined;
  onPageChange: (page: number) => void;
  onSort: (field: MediaSortField) => void;
  trailing?: ReactNode;
}

export function LibraryMediaTable({
  libraryId,
  libraryPath,
  items,
  isLoading,
  total,
  page,
  sortBy,
  order,
  onPageChange,
  onSort,
  trailing,
}: LibraryMediaTableProps) {
  const { t } = useTranslation(["library", "common", "metadata"]);
  const queryClient = useQueryClient();
  const limit = useUIStore((s) => s.pageSizes.libraryMedia);
  const { selected, selectedIds, toggleOne, toggleAll, isAllSelected, selectAll, invert, clear } =
    useIdSelection();
  const [batchScraping, setBatchScraping] = useState(false);
  const [batchOrganizing, setBatchOrganizing] = useState(false);
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [deleteTargets, setDeleteTargets] = useState<{ ids: number[]; workDir: boolean } | null>(
    null,
  );
  const [overrideTarget, setOverrideTarget] = useState<MediaFileResponse | null>(null);
  const [overrideSaving, setOverrideSaving] = useState(false);
  const [playId, setPlayId] = useState<number | null>(null);
  // 内嵌播放器的队列: 外部播放器未启用时, "播放"落到这里.
  const [queue, setQueue] = useState<readonly PlaylistEntry[]>([]);

  // 播放与海报墙同语义: 启用外部播放器就直接交给本机播放器, 否则打开内嵌播放弹窗.
  const localPlayer = useLocalPlayer();
  const openLocal = useOpenLocalPlayback();
  const appendToPlaylist = usePlaylistStore((state) => state.append);
  const openPlaylist = useAppMutation({
    mutationOptions: openLocalPlaylistMutation(),
    successToast: (result) =>
      t("metadata:playlist.openedExternal", {
        count: result.paths.length,
        player: result.player ?? t("metadata:detail.openLocalSystem"),
      }),
  });

  function handlePlay(item: MediaFileResponse) {
    const metadataId = item.metadata_id;
    if (metadataId == null) return;
    if (localPlayer.data?.enabled === true) {
      openLocal.mutate(
        { path: { metadata_id: metadataId } },
        {
          onError: (err) =>
            notifications.show({
              message: extractErrorMessage(err, t("common:toast.operationFailed")),
              color: "red",
            }),
        },
      );
    } else {
      setPlayId(metadataId);
    }
  }

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: listMediaQueryKey() });

  async function handleBatchScrape() {
    const ids = selectedIds;
    setBatchScraping(true);
    try {
      const { data } = await batchSubmitTasks({
        body: { submissions: ids.map((id) => ({ type: "scrape", media_id: id })) },
        throwOnError: true,
      });
      if (data.submitted > 0) {
        notifications.show({
          message: t("common:toast.batchScrapeStarted", { count: data.submitted }),
          color: "blue",
        });
      }
      if (data.missing > 0) {
        notifications.show({
          message: t("common:toast.batchScrapeFailed", { count: data.missing }),
          color: "red",
        });
      }
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.batchScrapeFailed")),
        color: "red",
      });
    } finally {
      setBatchScraping(false);
    }
    clear();
  }

  async function handleBatchOrganize() {
    const ids = selectedIds;
    setBatchOrganizing(true);
    try {
      await submitTask({
        body: { type: "organize", library_id: libraryId, media_file_ids: ids },
        throwOnError: true,
      });
      notifications.show({
        message: t("common:toast.organizeStarted"),
        color: "blue",
      });
      clear();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setBatchOrganizing(false);
    }
  }

  async function handleBatchDelete() {
    const ok = await confirm({
      title: t("batch.confirmDeleteTitle"),
      message: t("batch.confirmDeleteDesc", { count: selected.size }),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) return;
    const ids = selectedIds;
    setBatchDeleting(true);
    try {
      const { data } = await batchDeleteMedia({ body: { ids }, throwOnError: true });
      if (data.deleted > 0) {
        notifications.show({ message: t("common:toast.mediaDeleted"), color: "blue" });
      }
      if (data.missing > 0) {
        notifications.show({
          message: extractErrorMessage(null, t("common:toast.operationFailed")),
          color: "red",
        });
      }
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setBatchDeleting(false);
    }
    clear();
    invalidate();
  }

  async function handleOverrideScrape(number: string, contentType: ContentType | undefined) {
    if (overrideTarget == null) return;
    setOverrideSaving(true);
    try {
      await submitTask({
        body: {
          type: "scrape",
          media_id: overrideTarget.id,
          number,
          ...(contentType != null ? { content_type: contentType } : {}),
        },
        throwOnError: true,
      });
      notifications.show({
        message: t("common:toast.scrapeStarted"),
        color: "blue",
      });
      setOverrideTarget(null);
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    } finally {
      setOverrideSaving(false);
    }
  }

  async function handleDeleteOne(mediaId: number) {
    const ok = await confirm({
      title: t("detail.confirmDeleteTitle"),
      message: t("detail.confirmDeleteDesc"),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) return;
    await deleteMedia({ path: { media_id: mediaId } });
    notifications.show({ message: t("common:toast.mediaDeleted"), color: "blue" });
    invalidate();
  }

  const totalPages = Math.max(1, Math.ceil(total / limit));
  const pageIds = items.map((i) => i.id);

  /** 可加入队列的行: 未绑定条目的文件没有可播对象. */
  const queueEntries = items
    .filter((item) => selected.has(item.id))
    .map(entryFromMediaFile)
    .filter((entry): entry is PlaylistEntry => entry != null);

  function handlePlaySelected() {
    if (queueEntries.length === 0) return;
    if (localPlayer.data?.enabled === true) {
      openPlaylist.mutate({
        body: { metadata_ids: queueEntries.map((entry) => entry.metadataId) },
      });
      return;
    }
    setQueue(queueEntries);
  }

  function handleAddToPlaylist() {
    if (queueEntries.length === 0) return;
    const before = usePlaylistStore.getState().items.length;
    appendToPlaylist(queueEntries);
    const added = usePlaylistStore.getState().items.length - before;
    notifications.show({
      message:
        added === 0
          ? t("metadata:playlist.addedAllDuplicate")
          : t("metadata:playlist.added", { count: added }),
      color: added === 0 ? "gray" : "blue",
    });
  }
  const allSelected = isAllSelected(pageIds);
  const effectiveSortBy = sortBy ?? "updated_at";
  const effectiveOrder = order ?? "desc";
  const busy = batchScraping || batchOrganizing || batchDeleting;
  const playItem = playId != null ? items.find((item) => item.metadata_id === playId) : undefined;

  function openMediaDelete(ids: number[], includeWorkDir: boolean) {
    setDeleteTargets({ ids, workDir: includeWorkDir });
  }

  function handlePageChange(p: number) {
    clear();
    onPageChange(p);
  }

  return (
    <ListToolbar
      totalPages={totalPages}
      page={page}
      onChange={handlePageChange}
      header={
        <SelectionBar count={selected.size}>
          <HintedActionIcon
            label={t("metadata:playlist.selectAll")}
            variant="default"
            size="sm"
            onClick={() => selectAll(pageIds)}
          >
            <IconSelectAll size={16} />
          </HintedActionIcon>
          <HintedActionIcon
            label={t("metadata:playlist.invert")}
            variant="default"
            size="sm"
            onClick={() => invert(pageIds)}
          >
            <IconSquareOff size={16} />
          </HintedActionIcon>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconPlayerPlay size={14} />}
            loading={openPlaylist.isPending}
            disabled={selected.size === 0}
            onClick={handlePlaySelected}
          >
            {t("metadata:actions.playSelected")}
          </Button>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconPlaylistAdd size={14} />}
            disabled={selected.size === 0}
            onClick={handleAddToPlaylist}
          >
            {t("metadata:actions.addToPlaylist")}
          </Button>
          {/* 窄屏三个批量按钮分成多行, 占用表体高度: sm 以下改由菜单提供, 选中数仍由 Badge 常驻呈现. */}
          <Group gap="xs" wrap="wrap" visibleFrom="sm">
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} />}
              loading={busy}
              disabled={selected.size === 0}
              onClick={() => void handleBatchScrape()}
            >
              {t("actions.batchScrape")}
            </Button>
            <Button
              size="xs"
              variant="light"
              leftSection={<IconFolderDown size={14} />}
              loading={busy}
              disabled={selected.size === 0}
              onClick={() => void handleBatchOrganize()}
            >
              {t("actions.batchOrganize")}
            </Button>
            <Menu position="bottom-end" withinPortal>
              <Menu.Target>
                <Button
                  size="xs"
                  variant="light"
                  color="red"
                  leftSection={<IconTrash size={14} />}
                  loading={busy}
                  disabled={selected.size === 0}
                >
                  {t("cleanup.deleteMenu")}
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                <DeleteMenuItems
                  onDeleteRecord={() => void handleBatchDelete()}
                  onDeleteFiles={() => openMediaDelete(selectedIds, false)}
                  onDeleteWorkDir={() => openMediaDelete(selectedIds, true)}
                />
              </Menu.Dropdown>
            </Menu>
          </Group>
          <Box hiddenFrom="sm">
            <Menu position="bottom-start" withinPortal>
              <Menu.Target>
                <Button size="xs" variant="light" loading={busy} disabled={selected.size === 0}>
                  {t("columns.actions")}
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  leftSection={<IconRefresh size={14} />}
                  disabled={busy}
                  onClick={() => void handleBatchScrape()}
                >
                  {t("actions.batchScrape")}
                </Menu.Item>
                <Menu.Item
                  leftSection={<IconFolderDown size={14} />}
                  disabled={busy}
                  onClick={() => void handleBatchOrganize()}
                >
                  {t("actions.batchOrganize")}
                </Menu.Item>
                <Menu.Divider />
                <DeleteMenuItems
                  disabled={busy}
                  onDeleteRecord={() => void handleBatchDelete()}
                  onDeleteFiles={() => openMediaDelete(selectedIds, false)}
                  onDeleteWorkDir={() => openMediaDelete(selectedIds, true)}
                />
              </Menu.Dropdown>
            </Menu>
          </Box>
        </SelectionBar>
      }
      trailing={trailing}
    >
      <Table
        stickyHeader
        highlightOnHover
        verticalSpacing="sm"
        layout="fixed"
        w="100%"
        className={classes.table}
      >
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={40}>
              <Checkbox checked={allSelected} onChange={() => toggleAll(pageIds)} />
            </Table.Th>
            <Table.Th w={POSTER_WIDTH + 20}>{t("columns.poster")}</Table.Th>
            <Table.Th w={120} visibleFrom={METADATA_VISIBLE_FROM}>
              {t("columns.metadata")}
            </Table.Th>
            {SORTABLE_COLUMNS.map((field) => (
              <SortableTh
                key={field}
                field={field}
                label={t(`columns.${COLUMN_I18N_KEY[field]}`)}
                sortBy={effectiveSortBy}
                order={effectiveOrder}
                onSort={onSort}
                w={COLUMN_WIDTH[field]}
                visibleFrom={COLUMN_VISIBLE_FROM[field]}
              />
            ))}
            <Table.Th ta="right" className={classes.actionsColumn}>
              {t("columns.actions")}
            </Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {items.map((item) => {
            const rel = relativePath(libraryPath, item.path);
            return (
              <Table.Tr
                key={item.id}
                bg={selected.has(item.id) ? "var(--mantine-color-blue-light)" : undefined}
              >
                <Table.Td>
                  <Checkbox checked={selected.has(item.id)} onChange={() => toggleOne(item.id)} />
                </Table.Td>
                <Table.Td>
                  <RowPoster item={item} onPlay={handlePlay} />
                </Table.Td>
                <Table.Td style={CELL_OVERFLOW} visibleFrom={METADATA_VISIBLE_FROM}>
                  {item.metadata_id != null ? (
                    <Link
                      to="/meta/$metadataId"
                      params={{ metadataId: String(item.metadata_id) }}
                      style={{ textDecoration: "none", display: "block", overflow: "hidden" }}
                    >
                      <Text
                        component="span"
                        size="sm"
                        ff="monospace"
                        c="brand"
                        truncate
                        title={`#${item.metadata_id}`}
                      >
                        #{item.metadata_id}
                      </Text>
                    </Link>
                  ) : (
                    <Text size="sm" c="dimmed">
                      —
                    </Text>
                  )}
                </Table.Td>
                <Table.Td style={CELL_OVERFLOW}>
                  <Stack gap={4}>
                    <Text size="sm" ff="monospace" truncate title={rel}>
                      {rel}
                    </Text>
                    <FilePhaseBadges phase={item} />
                  </Stack>
                </Table.Td>
                <Table.Td style={CELL_OVERFLOW}>
                  <Badge size="sm" variant="light" color={statusColor(item.status)}>
                    {t(`filters.${item.status}`)}
                  </Badge>
                </Table.Td>
                <Table.Td style={CELL_OVERFLOW} visibleFrom={COLUMN_VISIBLE_FROM.size}>
                  <Text size="sm" truncate>
                    {formatFileSize(item.size)}
                  </Text>
                </Table.Td>
                <Table.Td style={CELL_OVERFLOW} visibleFrom={COLUMN_VISIBLE_FROM.updated_at}>
                  <Text size="sm" truncate>
                    {item.updated_at ? item.updated_at.slice(0, 10) : "—"}
                  </Text>
                </Table.Td>
                <Table.Td>
                  {/* 窄屏动作列只容得下一个按钮, 三个动作移入菜单. */}
                  <Group gap={4} justify="flex-end" wrap="nowrap" visibleFrom="sm">
                    <HintedActionIcon
                      variant="subtle"
                      label={t("actions.scrape")}
                      onClick={() =>
                        submitTask({ body: { type: "scrape", media_id: item.id } }).then(() =>
                          notifications.show({
                            message: t("common:toast.scrapeStarted"),
                            color: "blue",
                          }),
                        )
                      }
                    >
                      <IconRefresh size={16} />
                    </HintedActionIcon>
                    <HintedActionIcon
                      variant="subtle"
                      label={t("actions.scrapeWithNumber")}
                      onClick={() => setOverrideTarget(item)}
                    >
                      <IconForms size={16} />
                    </HintedActionIcon>
                    <Menu position="bottom-end" withinPortal>
                      <Menu.Target>
                        <ActionIcon
                          variant="subtle"
                          color="red"
                          size="sm"
                          aria-label={t("cleanup.deleteMenu")}
                        >
                          <IconTrash size={16} />
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        <DeleteMenuItems
                          onDeleteRecord={() => void handleDeleteOne(item.id)}
                          onDeleteFiles={() => openMediaDelete([item.id], false)}
                          onDeleteWorkDir={() => openMediaDelete([item.id], true)}
                        />
                      </Menu.Dropdown>
                    </Menu>
                  </Group>
                  {/* Menu 不接受 visibleFrom / hiddenFrom, 显隐由外层 Box 承担. */}
                  <Box hiddenFrom="sm" style={{ display: "flex", justifyContent: "flex-end" }}>
                    <Menu position="bottom-end" withinPortal>
                      <Menu.Target>
                        <ActionIcon size="sm" variant="subtle" aria-label={t("columns.actions")}>
                          <IconDots size={14} />
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Item
                          leftSection={<IconRefresh size={14} />}
                          onClick={() =>
                            submitTask({ body: { type: "scrape", media_id: item.id } }).then(() =>
                              notifications.show({
                                message: t("common:toast.scrapeStarted"),
                                color: "blue",
                              }),
                            )
                          }
                        >
                          {t("actions.scrape")}
                        </Menu.Item>
                        <Menu.Item
                          leftSection={<IconForms size={14} />}
                          onClick={() => setOverrideTarget(item)}
                        >
                          {t("actions.scrapeWithNumber")}
                        </Menu.Item>
                        <Menu.Divider />
                        <DeleteMenuItems
                          onDeleteRecord={() => void handleDeleteOne(item.id)}
                          onDeleteFiles={() => openMediaDelete([item.id], false)}
                          onDeleteWorkDir={() => openMediaDelete([item.id], true)}
                        />
                      </Menu.Dropdown>
                    </Menu>
                  </Box>
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>

      {!isLoading && items.length === 0 && (
        <Text c="dimmed" size="sm" ta="center" py="xl">
          {t("empty")}
        </Text>
      )}
      <ScrapeOverrideDialog
        target={overrideTarget}
        saving={overrideSaving}
        onClose={() => {
          if (!overrideSaving) setOverrideTarget(null);
        }}
        onSubmit={(number, contentType) => void handleOverrideScrape(number, contentType)}
      />
      <MediaDeleteDialog
        libraryId={libraryId}
        mediaFileIds={deleteTargets?.ids ?? []}
        includeWorkDir={deleteTargets?.workDir ?? false}
        opened={deleteTargets !== null}
        onClose={() => setDeleteTargets(null)}
        onDeleted={() => {
          clear();
          invalidate();
        }}
      />
      <PlaybackModal
        metadataId={playId}
        title={playItem?.number ?? ""}
        onClose={() => setPlayId(null)}
      />
      <PlaybackQueueModal
        entries={queue}
        title={queue.length === 1 ? queue[0].number : t("metadata:playlist.title")}
        onClose={() => setQueue([])}
      />
    </ListToolbar>
  );
}
