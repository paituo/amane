import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  Menu,
  Modal,
  Stack,
  Tabs,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import {
  IconAlertCircle,
  IconCheck,
  IconChevronRight,
  IconCrop,
  IconDotsVertical,
  IconExternalLink,
  IconFolders,
  IconGitMerge,
  IconLock,
  IconLockOpen,
  IconPencil,
  IconPhotoOff,
  IconPlayerPlay,
  IconRefresh,
  IconStar,
  IconTrash,
  IconX,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  batchMetadataUserTagsMutation,
  createUserTagsMutation,
  deleteMetadataMutation,
  getMetadataOptions,
  getMetadataQueryKey,
  listFacetsOptions,
  listFacetsQueryKey,
  listMetadataQueryKey,
  setMetadataLocksMutation,
  submitTaskMutation,
  updateMetadataMutation,
} from "@/client/@tanstack/react-query.gen";
import { getMetadataSchema } from "@/client/sdk.gen";
import type { MetadataField, MetadataResponse } from "@/client/types.gen";
import { FacetBadge } from "@/components/media/facet-badge";
import { LockChip, LockToggle, type LockProps } from "@/components/media/field-lock";
import { UserTagActions } from "@/components/media/user-tag-add";
import { VrBadge } from "@/components/media/vr-badge";
import { FanartLightbox } from "@/components/media/fanart-lightbox";
import { FilePhaseOverlay } from "@/components/media/file-phase-badges";
import { PosterCropDialog } from "@/components/media/poster-crop-dialog";
import { MergeDialog } from "@/components/metadata/merge-dialog";
import { type JSONSchemaObject, resolveSchema } from "@/components/schema-form/schema";
import { SchemaForm } from "@/components/schema-form/schema-form";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { USER_TAG_FACET_LIST } from "@/lib/facets";
import { hasFavoriteTag, useFavoriteMutation, useFavoriteTagId } from "@/lib/media/favorites";
import { FIELD_LABEL_KEY, LOCKABLE_FIELDS } from "@/lib/media/metadata-fields";
import { proxyImageUrl } from "@/lib/utils";
import { ProxyImage } from "@/components/media/proxy-image";
import { CommentSection } from "@/components/media/comment-section";
import { PlaybackPanel } from "@/components/media/playback-panel";
import { useLocalPlayer, useOpenLocalPlayback } from "@/lib/media/local-player";
import type { SeekRequest } from "@/components/media/playback-player";

function formatRuntime(minutes?: number | null): string | null {
  if (!minutes) return null;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function FieldBlock({
  label,
  children,
  lock,
}: {
  label: string;
  children: ReactNode;
  lock?: LockProps<MetadataField>;
}) {
  return (
    <div>
      <Group gap={4} wrap="nowrap" align="center">
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        {lock && <LockToggle {...lock} />}
      </Group>
      <div>{children}</div>
    </div>
  );
}

/** 视频信息 Tab 的占位字段: 后端媒体探测接口接入前先以空值陈列, 不读取真实数据. */
const VIDEO_INFO_FIELDS = [
  "视频格式",
  "时长",
  "帧率",
  "总码率",
  "分辨率",
  "宽高比",
  "位深度",
  "总帧数",
  "音频格式",
  "音频码率",
  "采样率",
  "声道数",
  "文件后缀",
  "文件大小",
];

export interface MetadataDetailProps {
  /** 路由参数原值: 详情路由把它写在地址栏上, 弹窗场景取自列表里的 id. */
  metadataId: string;
  /** 地址栏带来的跳转秒数; 未提供时不接受外部跳转请求. */
  seekSeconds?: number | null;
  /** 跳转写回地址栏 (详情路由); 不传时跳转只在本组件内生效. */
  onSeekCommit?: (seconds: number) => void;
  /** 删除后由宿主接管 (弹窗关闭); 不传时退回片库列表. */
  onDeleted?: () => void;
  /** 弹窗场景: 滚动由宿主的容器负责, 不再补整页下沿留白. */
  embedded?: boolean;
  /** 弹窗场景: 翻阅位置 (如「19 / 60」). 由标题行承载, 宿主不再另开一行放置. */
  counter?: string;
  /** 弹窗场景: 标题行末尾的关闭按钮; 页面场景不传, 省掉这一行也省掉弹窗自带标题栏. */
  onClose?: () => void;
}

/** 影片详情主体: 详情路由与就地弹窗共用同一份渲染. */
export function MetadataDetail({
  metadataId,
  seekSeconds = null,
  onSeekCommit,
  onDeleted,
  embedded = false,
  counter,
  onClose,
}: MetadataDetailProps) {
  const { t } = useTranslation(["metadata", "library", "common"]);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [editOpen, setEditOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [cropOpen, setCropOpen] = useState(false);
  const [coverLightboxOpen, coverLightbox] = useDisclosure(false);
  const [thumbBroken, setThumbBroken] = useState(false);
  const [posterBroken, setPosterBroken] = useState(false);
  /** 播放请求计数: 封面按钮与就地播放面板之间的信号, 面板看到它变化就开始播放. */
  const [playNonce, setPlayNonce] = useState(0);
  // 外部播放器: 开着时封面按钮直接拉起本机播放器; 关着时把播放请求交给封面处的就地播放面板.
  const localPlayer = useLocalPlayer();
  const openLocal = useOpenLocalPlayback();
  const external = localPlayer.data?.enabled === true;
  // 默认不显示播放面板; 仅在外部播放器关闭、且点过封面播放按钮 (playNonce>0) 后, 才在封面原位就地播放.
  const showEmbedded = external === false && playNonce > 0;
  // 评论时间戳的跳转: 本地请求负责即时响应 (同一秒连点也要重新触发), 地址栏的 `t` 负责分享与刷新定位.
  const [seekRequest, setSeekRequest] = useState<SeekRequest | null>(null);
  const [canSeek, setCanSeek] = useState(false);
  const seekFromUrlRef = useRef<number | null>(null);
  // 跳转请求在处理之后会被清空, 因此 nonce 不能取自请求本身: 每次都从 1 重新计数时, 播放器会把
  // 新请求当成已处理的那一条而忽略. 计数只增不减, 见 `SeekRequest`.
  const seekNonceRef = useRef(0);

  useEffect(() => {
    if (seekSeconds == null || seekFromUrlRef.current === seekSeconds) {
      return;
    }
    seekFromUrlRef.current = seekSeconds;
    seekNonceRef.current += 1;
    setSeekRequest({ seconds: seekSeconds, nonce: seekNonceRef.current });
  }, [seekSeconds]);

  const requestSeek = useCallback(
    (seconds: number) => {
      seekFromUrlRef.current = seconds;
      seekNonceRef.current += 1;
      setSeekRequest({ seconds, nonce: seekNonceRef.current });
      // 写地址栏是详情路由的事 (分享与刷新后定位); 弹窗场景不传该回调, 跳转只在这次挂载内生效.
      onSeekCommit?.(seconds);
    },
    [onSeekCommit],
  );

  const id = Number(metadataId);
  const validId = Number.isInteger(id) && id > 0;
  // 剧照画廊的当前图. 声明在下面那段"渲染期同步"之前, 换片时才能一起复位.
  const [activeIdx, setActiveIdx] = useState(0);
  const [prevId, setPrevId] = useState(id);
  if (id !== prevId) {
    setPrevId(id);
    setThumbBroken(false);
    setPosterBroken(false);
    setPlayNonce(0);
    // 换片回到主图: 渲染期直接调整比 useEffect 少一轮级联渲染.
    setActiveIdx(0);
  }

  const { data, isLoading, isError } = useQuery({
    ...getMetadataOptions({ path: { metadata_id: id } }),
    enabled: validId,
  });

  const invalidateDetail = () => {
    void queryClient.invalidateQueries({
      queryKey: getMetadataQueryKey({ path: { metadata_id: id } }),
    });
  };

  const scrapeMutation = useMutation({
    ...submitTaskMutation(),
    onSuccess: () => {
      notifications.show({ message: t("common:toast.scrapeStarted"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  const deleteMutation = useMutation({
    ...deleteMetadataMutation(),
    onSuccess: () => {
      notifications.show({ message: t("common:toast.metadataDeleted"), color: "blue" });
      void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
      // 详情页自行退回列表; 弹窗场景由宿主关闭, 列表仍在原处不动.
      if (onDeleted) {
        onDeleted();
        return;
      }
      void navigate({ to: "/meta" });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  async function handleDelete() {
    const ok = await confirm({
      title: t("common:actions.delete"),
      message: t("confirmDelete", { id }),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) return;
    deleteMutation.mutate({ path: { metadata_id: id } });
  }

  const { data: userTagOptions } = useQuery(listFacetsOptions(USER_TAG_FACET_LIST));
  const { data: rawSchema } = useQuery({
    queryKey: ["metadata-schema"],
    queryFn: async () => {
      const { data: schemaData } = await getMetadataSchema();
      // OpenAPI schema 运行时对象 → Schema 表单内部类型.
      return schemaData as JSONSchemaObject;
    },
  });
  const editSchema = useMemo(
    () => (rawSchema ? resolveSchema(rawSchema, rawSchema) : null),
    [rawSchema],
  );

  const updateMutation = useMutation({
    ...updateMetadataMutation(),
    onSuccess: () => {
      notifications.show({ message: t("common:toast.metadataUpdated"), color: "blue" });
      setEditOpen(false);
      invalidateDetail();
      void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  const ensureTagsMutation = useMutation(createUserTagsMutation());
  const applyTagsMutation = useMutation(batchMetadataUserTagsMutation());
  const favoriteTagId = useFavoriteTagId().data ?? null;
  const favoriteMutation = useFavoriteMutation();
  const isFavorite = hasFavoriteTag(data?.user_tags ?? [], favoriteTagId);

  const [pendingLock, setPendingLock] = useState<MetadataField | null>(null);
  const [lockTarget, setLockTarget] = useState<{ id: number; fields: MetadataField[] } | null>(
    null,
  );
  // 锁写入串行化: 在途点击合并为最后目标, 结算后补发.
  const queuedLocksRef = useRef<{ id: number; fields: MetadataField[] } | null>(null);
  const lockMutation = useMutation({
    ...setMetadataLocksMutation(),
    onSuccess: async (_data, variables) => {
      // 失效按本次请求的条目; 等重取完成再撤销乐观显示, 避免图标回退翻转.
      await queryClient.invalidateQueries({
        queryKey: getMetadataQueryKey({ path: { metadata_id: variables.path.metadata_id } }),
      });
    },
    onError: (err) => {
      queuedLocksRef.current = null;
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    },
    onSettled: () => {
      const queued = queuedLocksRef.current;
      queuedLocksRef.current = null;
      if (queued) {
        // 条目已切换时按原条目补发.
        lockMutation.mutate({ path: { metadata_id: queued.id }, body: { fields: queued.fields } });
        return;
      }
      setPendingLock(null);
      setLockTarget(null);
    },
  });
  const serverLockedFields = useMemo(
    () => new Set<MetadataField>(data?.metadata.locked_fields ?? []),
    [data?.metadata.locked_fields],
  );
  // 乐观显示目标集合; 在途状态不扩散到其它锁控件.
  const lockedFields = useMemo(
    () =>
      lockTarget && lockTarget.id === id
        ? new Set<MetadataField>(lockTarget.fields)
        : serverLockedFields,
    [lockTarget, id, serverLockedFields],
  );

  async function handleAddTags(selection: { tagIds: number[]; createNames: string[] }) {
    const names = [
      ...new Set(
        selection.createNames.map((name) => name.trim()).filter((name) => name.length > 0),
      ),
    ];
    if (selection.tagIds.length === 0 && names.length === 0) return;
    try {
      // 新建与挂载各一次请求: 名称 → id 由批量创建端点取回, 已存在的名称直接复用
      let createdIds: number[] = [];
      let created = 0;
      if (names.length > 0) {
        const ensured = await ensureTagsMutation.mutateAsync({ body: { names } });
        createdIds = ensured.items.map((tag) => tag.id);
        created = ensured.created;
        // 标签此刻已落库; created 为 0 也可能是别人刚建的同名行落在候选之外, 故一律重取
        void queryClient.invalidateQueries({ queryKey: listFacetsQueryKey(USER_TAG_FACET_LIST) });
      }
      await applyTagsMutation.mutateAsync({
        body: { ids: [id], user_tag_ids: [...selection.tagIds, ...createdIds], action: "attach" },
      });
      notifications.show({
        message: created > 0 ? t("common:toast.userTagCreated") : t("common:toast.userTagAttached"),
        color: "blue",
      });
      invalidateDetail();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    }
  }

  async function handleDetachTags(tagIds: number[]) {
    if (tagIds.length === 0) return;
    try {
      await applyTagsMutation.mutateAsync({
        body: { ids: [id], user_tag_ids: tagIds, action: "detach" },
      });
      notifications.show({ message: t("common:toast.userTagDetached"), color: "blue" });
      invalidateDetail();
    } catch (err) {
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      });
    }
  }

  // 剧照画廊: 这三个 hook 必须在 early return 之前声明, 否则 loading→loaded 两次渲染的 hook
  // 数量不一致, React 会抛 #310 (Rendered more hooks than during the previous render).
  const galleryImageSrc = useMemo(() => {
    const m = data?.metadata;
    if (!m) return [] as string[];
    const thumb = m.thumb_url ?? m.thumb_urls?.[0] ?? null;
    const poster = m.poster_url ?? m.poster_urls?.[0] ?? null;
    const cover = thumb && !thumbBroken ? thumb : poster && !posterBroken ? poster : null;
    const coverUrl = proxyImageUrl(cover);
    const list: string[] = [];
    if (coverUrl) list.push(coverUrl);
    for (const u of m.extrafanart ?? []) {
      const prox = proxyImageUrl(u) ?? u;
      if (prox) list.push(prox);
    }
    return list;
  }, [data, thumbBroken, posterBroken]);

  if (!validId) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={18} />}>
        {t("invalidId")}
      </Alert>
    );
  }

  if (isLoading) {
    return <Loader />;
  }

  if (isError || !data) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={18} />}>
        {t("notFound")}
      </Alert>
    );
  }

  const item: MetadataResponse = data.metadata;
  // 详情只展示一张主图: 优先横版封面 (thumb), 失败或不存在时回退竖版海报.
  const thumbSrc = item.thumb_url ?? item.thumb_urls?.[0] ?? null;
  const posterSrc = item.poster_url ?? item.poster_urls?.[0] ?? null;
  const coverSrc =
    thumbSrc && !thumbBroken ? thumbSrc : posterSrc && !posterBroken ? posterSrc : null;
  const coverUrl = proxyImageUrl(coverSrc);
  const coverFailed = Boolean((thumbSrc || posterSrc) && !coverUrl);
  const runtime = formatRuntime(item.runtime);
  const firstLibraryId = data.files?.[0]?.library_id ?? null;
  // 剧照画廊当前激活图 (hook 已上移至 early return 之前, 这里只取值).
  const activeImage =
    galleryImageSrc.length > 0
      ? galleryImageSrc[Math.min(activeIdx, galleryImageSrc.length - 1)]
      : null;
  const allLocked = LOCKABLE_FIELDS.every((field) => lockedFields.has(field));

  function applyLocks(fields: readonly MetadataField[], pending: MetadataField | null = null) {
    const target = [...fields];
    setPendingLock(pending);
    setLockTarget({ id, fields: target });
    if (lockMutation.isPending) {
      queuedLocksRef.current = { id, fields: target };
      return;
    }
    lockMutation.mutate({ path: { metadata_id: id }, body: { fields: target } });
  }

  function toggleLock(field: MetadataField) {
    const next = new Set(lockedFields);
    if (next.has(field)) {
      next.delete(field);
    } else {
      next.add(field);
    }
    applyLocks(
      LOCKABLE_FIELDS.filter((candidate) => next.has(candidate)),
      field,
    );
  }

  const lockProps = (field: MetadataField): LockProps<MetadataField> => ({
    field,
    locked: lockedFields.has(field),
    busy: pendingLock === field,
    onToggleLock: toggleLock,
  });

  function handleCoverError() {
    if (coverSrc && coverSrc === thumbSrc) {
      setThumbBroken(true);
      return;
    }
    if (coverSrc && coverSrc === posterSrc) {
      setPosterBroken(true);
    }
  }

  // 封面上的大按钮 = 播正片. 外部播放器开着就交给它; 关着就把请求发给下方的播放面板 (滚动过去并开始播放).
  function startPlayback() {
    if (localPlayer.data?.enabled !== true) {
      setPlayNonce((nonce) => nonce + 1);
      return;
    }
    openLocal.mutate(
      { path: { metadata_id: id } },
      {
        onSuccess: (result) => {
          notifications.show({
            message: t("detail.openLocalDone", {
              player: result.player ?? t("detail.openLocalSystem"),
            }),
            color: "blue",
          });
        },
        onError: (failure) => {
          notifications.show({
            message: extractErrorMessage(failure, t("detail.openLocalFailed")),
            color: "red",
          });
        },
      },
    );
  }

  const coverPlayButton = (
    <ActionIcon
      variant="filled"
      color="dark"
      radius="xl"
      size={56}
      loading={openLocal.isPending}
      aria-label={t("detail.play")}
      // 封面容器同时承担「点空白看大图」: 播放按钮必须自己截断冒泡, 否则一次点击既起播又开灯箱.
      onClick={(event) => {
        event.stopPropagation();
        startPlayback();
      }}
      style={{
        position: "absolute",
        top: "50%",
        left: "50%",
        transform: "translate(-50%, -50%)",
        opacity: 0.88,
        zIndex: 1,
      }}
    >
      <IconPlayerPlay size={28} />
    </ActionIcon>
  );

  return (
    // 下沿留出的空白比其余三边大得多: 滚到底时末尾的卡片不贴视口底边, 还能再滚一截.
    // 弹窗自带滚动容器, 这份留白会变成多余的空白, 交给 `embedded` 关掉.
    <Stack gap="md" pb={embedded ? undefined : "calc(var(--amane-vh) * 0.1)"}>
      <Group justify="space-between" align="center" wrap="wrap" gap="xs">
        {/* flex-basis 取 0 而不是内容宽度: 标题再长也只吃剩余空间, 不会把右侧动作按钮挤到下一行. */}
        <Group gap={6} align="center" wrap="nowrap" style={{ flex: "1 1 0%", minWidth: 0 }}>
          {counter != null && (
            <Text size="xs" c="dimmed" ff="monospace" style={{ flexShrink: 0 }}>
              {counter}
            </Text>
          )}
          {/* 编号是与标签同排的定宽块: 窄视口下允许它把标题挤成省略号, 但不许在连字符处折行. */}
          <Title order={5} ff="monospace" style={{ flexShrink: 0, whiteSpace: "nowrap" }}>
            {item.number}
          </Title>
          {item.title && (
            <Text size="sm" c="dimmed" truncate>
              {item.title}
            </Text>
          )}
        </Group>
        <Group gap={4} wrap="nowrap">
          <Button
            size="xs"
            variant={isFavorite ? "light" : "default"}
            color={isFavorite ? "yellow" : undefined}
            loading={favoriteMutation.isPending}
            aria-pressed={isFavorite}
            leftSection={
              isFavorite ? <IconStar size={14} fill="currentColor" /> : <IconStar size={14} />
            }
            onClick={() => favoriteMutation.mutate({ metadataId: id, favorite: !isFavorite })}
          >
            {isFavorite ? t("favorite.remove") : t("favorite.add")}
          </Button>
          <Button
            size="xs"
            variant="filled"
            leftSection={<IconPlayerPlay size={14} />}
            onClick={startPlayback}
          >
            {t("detail.play")}
          </Button>
          <Menu shadow="md" position="bottom-end" withinPortal>
            <Menu.Target>
              <Button
                size="xs"
                variant="light"
                leftSection={<IconRefresh size={14} />}
                loading={scrapeMutation.isPending}
              >
                {t("actions.scrape")}
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Item
                onClick={() =>
                  scrapeMutation.mutate({ body: { type: "scrape", number: item.number } })
                }
              >
                <Text size="sm">{t("common:actions.scrapeNormal")}</Text>
                <Text size="xs" c="dimmed">
                  {t("common:actions.scrapeNormalDesc")}
                </Text>
              </Menu.Item>
              <Menu.Item
                onClick={() =>
                  scrapeMutation.mutate({
                    body: { type: "scrape", number: item.number, use_cache: [] },
                  })
                }
              >
                <Text size="sm">{t("common:actions.scrapeForce")}</Text>
                <Text size="xs" c="dimmed">
                  {t("common:actions.scrapeForceDesc")}
                </Text>
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconPencil size={14} />}
            onClick={() => setEditOpen(true)}
          >
            {t("common:actions.edit")}
          </Button>
          {firstLibraryId != null && (
            <ActionIcon
              variant="subtle"
              size="md"
              aria-label={t("detail.openInLibrary")}
              onClick={() =>
                void navigate({
                  to: "/libraries/$libraryId",
                  params: { libraryId: String(firstLibraryId) },
                  search: { q: item.number },
                })
              }
            >
              <IconFolders size={18} />
            </ActionIcon>
          )}
          <Menu shadow="md" position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon variant="subtle" size="md" aria-label={t("detail.more")}>
                <IconDotsVertical size={18} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu position="left-start" trigger="hover" withinPortal>
                <Menu.Target>
                  <Menu.Item
                    leftSection={<IconLock size={14} />}
                    rightSection={<IconChevronRight size={14} />}
                  >
                    {t("detail.lockManage")}
                  </Menu.Item>
                </Menu.Target>
                <Menu.Dropdown>
                  <Menu.Item onClick={() => applyLocks(allLocked ? [] : LOCKABLE_FIELDS)}>
                    {allLocked ? t("lock.clearAll") : t("lock.all")}
                  </Menu.Item>
                  <Menu.Divider />
                  {LOCKABLE_FIELDS.map((field) => (
                    <Menu.Item
                      key={field}
                      onClick={() => toggleLock(field)}
                      leftSection={
                        lockedFields.has(field) ? (
                          <IconLock size={14} />
                        ) : (
                          <IconLockOpen size={14} />
                        )
                      }
                      rightSection={lockedFields.has(field) ? <IconCheck size={14} /> : undefined}
                    >
                      {t(FIELD_LABEL_KEY[field])}
                    </Menu.Item>
                  ))}
                </Menu.Dropdown>
              </Menu>
              <Menu.Divider />
              <Menu.Item
                leftSection={<IconGitMerge size={14} />}
                onClick={() => setMergeOpen(true)}
              >
                {t("merge.title")}
              </Menu.Item>
              <Menu.Item
                color="red"
                leftSection={<IconTrash size={14} />}
                onClick={() => void handleDelete()}
              >
                {t("common:actions.delete")}
              </Menu.Item>
            </Menu.Dropdown>
          </Menu>
          {onClose && (
            <ActionIcon
              variant="subtle"
              size="md"
              aria-label={t("common:actions.close")}
              onClick={onClose}
            >
              <IconX size={18} />
            </ActionIcon>
          )}
        </Group>
      </Group>

      {/* 分栏方向只由 DOM 顺序表达: 封面列排在前, 就在左; 信息列在后, 就在右.
          别改回 `flexDirection: row-reverse` —— 内联样式确实生效 (实测封面会翻到右侧), 但读代码的人
          看到 reverse 会先入为主以为"封面在右", 与 DOM 顺序打架; 换行 (wrap) 时行序判断也更绕. */}
      <Group align="flex-start" wrap="wrap" gap="lg">
        <Stack gap="xs" style={{ flex: "3 1 360px", minWidth: 280 }}>
          {showEmbedded ? (
            <PlaybackPanel
              metadataId={id}
              playNonce={playNonce}
              seekRequest={seekRequest}
              onSeekHandled={() => setSeekRequest(null)}
              onCanSeekChange={setCanSeek}
            />
          ) : (
            <div
              // 点封面看大图由容器承担, 不另铺一层铺满的透明按钮: 那层按钮会连同播放按钮一起
              // 接住点击, 层级顺序一变就退化成"点播放却打开大图". 播放按钮自己截断冒泡.
              onClick={activeImage ? () => coverLightbox.open() : undefined}
              // 容器替下原来的透明按钮, 键盘入口要一并补回 (播放按钮仍是容器内的独立按钮).
              role={activeImage ? "button" : undefined}
              tabIndex={activeImage ? 0 : undefined}
              aria-label={activeImage ? item.number : undefined}
              onKeyDown={(event) => {
                if (!coverUrl) return;
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                coverLightbox.open();
              }}
              style={{
                position: "relative",
                width: "100%",
                borderRadius: "var(--mantine-radius-md)",
                overflow: "hidden",
                background: "var(--mantine-color-default-hover)",
                cursor: coverUrl ? "zoom-in" : undefined,
                // 无可展示封面时用 16:9 占位; 有封面则由 img 撑住尺寸.
                ...(!coverUrl ? { aspectRatio: "16 / 9" as const } : {}),
              }}
            >
              {activeImage && (
                <ProxyImage
                  key={activeImage}
                  src={activeImage}
                  alt={item.number}
                  referrerPolicy="no-referrer"
                  onError={handleCoverError}
                  style={{ width: "100%", height: "auto", display: "block" }}
                  placeholder={<div style={{ aspectRatio: "16 / 9" }} aria-hidden />}
                />
              )}
              <FilePhaseOverlay phase={item.file_phase} />

              {activeImage ? (
                coverPlayButton
              ) : (
                <div
                  style={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: 10,
                    padding: 16,
                  }}
                >
                  <IconPhotoOff
                    size={36}
                    stroke={1.25}
                    color="var(--mantine-color-dimmed)"
                    aria-hidden
                  />
                  <Text size="sm" ff="monospace" c="dimmed" ta="center">
                    {item.number}
                  </Text>
                  {coverFailed && (
                    <Text size="xs" c="dimmed" ta="center">
                      {t("detail.coverUnavailable")}
                    </Text>
                  )}
                  <ActionIcon
                    variant="filled"
                    color="dark"
                    radius="xl"
                    size={48}
                    loading={openLocal.isPending}
                    aria-label={t("detail.play")}
                    onClick={startPlayback}
                    style={{ opacity: 0.88 }}
                  >
                    <IconPlayerPlay size={24} />
                  </ActionIcon>
                </div>
              )}
            </div>
          )}

          {galleryImageSrc.length > 1 && (
            <Group
              gap={4}
              wrap="nowrap"
              style={{ overflowX: "auto", overflowY: "hidden", paddingBottom: 2 }}
            >
              {galleryImageSrc.map((src, i) => (
                <Box
                  key={i}
                  component="button"
                  type="button"
                  onClick={() => setActiveIdx(i)}
                  aria-label={`${t("detail.fields.extrafanart")} ${i + 1}`}
                  style={{
                    padding: 0,
                    border:
                      activeIdx === i
                        ? "2px solid var(--mantine-color-brand-5)"
                        : "2px solid transparent",
                    borderRadius: 4,
                    cursor: "pointer",
                    background: "none",
                    lineHeight: 0,
                    flexShrink: 0,
                  }}
                >
                  <ProxyImage
                    src={src}
                    alt={`thumb-${i}`}
                    referrerPolicy="no-referrer"
                    style={{
                      width: 88,
                      height: 60,
                      objectFit: "cover",
                      display: "block",
                      borderRadius: 4,
                    }}
                    placeholder={
                      <span style={{ width: 88, height: 60, display: "block" }} aria-hidden />
                    }
                  />
                </Box>
              ))}
            </Group>
          )}

          <Group gap="xs" wrap="wrap">
            <Tooltip label={t("detail.cropPoster.noThumb")} disabled={thumbSrc != null}>
              <Box display="inline-block">
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconCrop size={14} />}
                  disabled={!thumbSrc}
                  onClick={() => setCropOpen(true)}
                >
                  {t("detail.cropPoster.action")}
                </Button>
              </Box>
            </Tooltip>
            <LockChip label={t("detail.fields.thumb")} {...lockProps("thumb_urls")} />
            <LockChip label={t("detail.fields.poster")} {...lockProps("poster_urls")} />
            <LockChip label={t("detail.fields.trailer")} {...lockProps("trailer_urls")} />
          </Group>
        </Stack>

        <Stack gap="sm" style={{ flex: "2 1 280px", minWidth: 260 }}>
          <Tabs defaultValue="movie">
            <Tabs.List mb="sm">
              <Tabs.Tab value="movie">{t("detail.tabs.movie")}</Tabs.Tab>
              <Tabs.Tab value="video">{t("detail.tabs.video")}</Tabs.Tab>
              <Tabs.Tab value="comments">{t("detail.tabs.comments")}</Tabs.Tab>
            </Tabs.List>
            <Tabs.Panel value="movie">
              {item.title && (
                <Group gap={4} align="center" wrap="nowrap" mb={4}>
                  <Text c="dimmed">{item.title}</Text>
                  <LockToggle {...lockProps("title")} />
                </Group>
              )}

              <Group gap="lg" wrap="wrap">
                {item.studio && (
                  <FieldBlock label={t("detail.fields.studio")} lock={lockProps("studio")}>
                    <FacetBadge kind="studio" id={data.studio_id} name={item.studio} />
                  </FieldBlock>
                )}
                {item.publisher && (
                  <FieldBlock label={t("detail.fields.publisher")} lock={lockProps("publisher")}>
                    <FacetBadge kind="publisher" id={data.publisher_id} name={item.publisher} />
                  </FieldBlock>
                )}
                {item.series && (
                  <FieldBlock label={t("detail.fields.series")} lock={lockProps("series")}>
                    <FacetBadge kind="series" id={data.series_id} name={item.series} />
                  </FieldBlock>
                )}
                {item.release && (
                  <FieldBlock label={t("detail.fields.release")} lock={lockProps("release")}>
                    <Text size="sm">{item.release}</Text>
                  </FieldBlock>
                )}
                {runtime && (
                  <FieldBlock label={t("detail.fields.runtime")} lock={lockProps("runtime")}>
                    <Text size="sm">{runtime}</Text>
                  </FieldBlock>
                )}
                {item.score != null && (
                  <FieldBlock label={t("detail.fields.score")} lock={lockProps("score")}>
                    <Badge color="yellow" variant="light" leftSection={<IconStar size={12} />}>
                      {item.score.toFixed(1)}
                    </Badge>
                  </FieldBlock>
                )}
                {item.vr && (
                  <FieldBlock label={t("detail.fields.vr")}>
                    <VrBadge />
                  </FieldBlock>
                )}
              </Group>

              {item.directors && item.directors.length > 0 && (
                <FieldBlock label={t("detail.fields.directors")} lock={lockProps("directors")}>
                  <Group gap={6}>
                    {item.directors.map((d) => (
                      <FacetBadge key={d} kind="director" id={data.director_ids?.[d]} name={d} />
                    ))}
                  </Group>
                </FieldBlock>
              )}

              {item.actors && item.actors.length > 0 && (
                <FieldBlock label={t("detail.fields.actors")} lock={lockProps("actors")}>
                  <Group gap={6}>
                    {item.actors.map((a) => (
                      <FacetBadge
                        key={a}
                        kind="actor"
                        id={data.actor_ids?.[a]}
                        name={a}
                        gender={data.actor_genders?.[a]}
                      />
                    ))}
                  </Group>
                </FieldBlock>
              )}

              {item.tags && item.tags.length > 0 && (
                <FieldBlock label={t("detail.fields.tags")} lock={lockProps("tags")}>
                  <Group gap={6}>
                    {item.tags.map((tg) => (
                      <FacetBadge key={tg} kind="tag" id={data.tag_ids?.[tg]} name={tg} />
                    ))}
                  </Group>
                </FieldBlock>
              )}

              <FieldBlock label={t("detail.userTags")}>
                <Group gap={6} align="center" wrap="wrap">
                  {(data.user_tags ?? []).map((tag) => (
                    <FacetBadge key={tag.id} kind="user_tag" id={tag.id} name={tag.name} />
                  ))}
                  <UserTagActions
                    attached={data.user_tags ?? []}
                    candidates={(userTagOptions?.items ?? []).filter(
                      (tag) => !(data.user_tags ?? []).some((attached) => attached.id === tag.id),
                    )}
                    onChoose={(selection) => void handleAddTags(selection)}
                    onDetach={(ids) => void handleDetachTags(ids)}
                    disabled={ensureTagsMutation.isPending || applyTagsMutation.isPending}
                  />
                </Group>
              </FieldBlock>

              {item.source_urls && Object.keys(item.source_urls).length > 0 && (
                <FieldBlock label={t("detail.fields.sourceUrls")}>
                  <Group gap={6}>
                    {Object.entries(item.source_urls).flatMap(([site, raw]) => {
                      if (typeof raw !== "string" || !/^https?:\/\//i.test(raw)) return [];
                      return [
                        <Badge
                          key={site}
                          component="a"
                          href={raw}
                          target="_blank"
                          rel="noreferrer"
                          variant="outline"
                          color="gray"
                          size="sm"
                          rightSection={<IconExternalLink size={12} />}
                          style={{
                            cursor: "pointer",
                            textTransform: "none",
                            textDecoration: "none",
                          }}
                        >
                          {site}
                        </Badge>,
                      ];
                    })}
                  </Group>
                </FieldBlock>
              )}

              {item.plot && (
                <FieldBlock label={t("detail.fields.plot")} lock={lockProps("plot")}>
                  <Text size="sm" style={{ whiteSpace: "pre-line" }}>
                    {item.plot}
                  </Text>
                </FieldBlock>
              )}
            </Tabs.Panel>
            <Tabs.Panel value="video">
              <Text size="xs" c="dimmed" mb="xs">
                {t("detail.videoPending")}
              </Text>
              <Stack gap={6}>
                {VIDEO_INFO_FIELDS.map((label) => (
                  <Group key={label} gap="sm" wrap="nowrap">
                    <Text size="xs" c="dimmed" style={{ width: 96, flexShrink: 0 }}>
                      {label}
                    </Text>
                    <Text size="sm" c="dimmed">
                      —
                    </Text>
                  </Group>
                ))}
              </Stack>
            </Tabs.Panel>
            <Tabs.Panel value="comments">
              <CommentSection
                metadataId={id}
                comments={data.comments ?? []}
                canSeek={canSeek}
                onSeek={requestSeek}
              />
            </Tabs.Panel>
          </Tabs>
        </Stack>
      </Group>

      <Modal
        opened={editOpen}
        onClose={() => setEditOpen(false)}
        title={`${t("edit.title")} — ${item.number}`}
        size="72rem"
        styles={{ body: { paddingBottom: 88 } }}
      >
        {editSchema ? (
          <SchemaForm
            schema={editSchema}
            prefix="editor"
            i18nPrefix="metadata"
            fieldLayout="grid"
            actionsPlacement="affix"
            values={(() => {
              const bag: Record<string, unknown> = { ...item };
              const out: Record<string, unknown> = {};
              for (const key of Object.keys(editSchema.properties ?? {})) {
                out[key] = bag[key] ?? null;
              }
              return out;
            })()}
            onSave={(patch) => updateMutation.mutate({ path: { metadata_id: id }, body: patch })}
            saving={updateMutation.isPending}
          />
        ) : (
          <Text size="sm" c="dimmed">
            {t("common:status.loading")}
          </Text>
        )}
      </Modal>

      <MergeDialog
        metadata={item}
        opened={mergeOpen}
        onClose={() => setMergeOpen(false)}
        onMerged={invalidateDetail}
      />

      {thumbSrc && (
        <PosterCropDialog
          opened={cropOpen}
          onClose={() => setCropOpen(false)}
          metadataId={id}
          thumbUrl={thumbSrc}
          onSuccess={() => {
            invalidateDetail();
            void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
          }}
        />
      )}

      {coverLightboxOpen && galleryImageSrc.length > 0 && (
        <FanartLightbox
          images={galleryImageSrc}
          initialIndex={Math.min(activeIdx, galleryImageSrc.length - 1)}
          onClose={coverLightbox.close}
        />
      )}
    </Stack>
  );
}
