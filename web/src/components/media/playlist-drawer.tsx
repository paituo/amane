import {
  ActionIcon,
  Box,
  Center,
  Checkbox,
  Divider,
  Drawer,
  Group,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconArrowDown,
  IconArrowUp,
  IconPlayerPlay,
  IconSelectAll,
  IconSquareOff,
  IconTrash,
  IconX,
} from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { openLocalPlaylistMutation } from "@/client/@tanstack/react-query.gen";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { PlaybackQueueModal } from "@/components/media/playback-queue-modal";
import { ProxyImage } from "@/components/media/proxy-image";
import { useAppMutation } from "@/hooks/use-app-mutation";
import { useLocalPlayer } from "@/lib/media/local-player";
import { proxyImageUrl } from "@/lib/utils";
import { type PlaylistEntry, usePlaylistStore } from "@/stores/playlist";

const THUMB_WIDTH = 40;
const THUMB_HEIGHT = 56;

function Row({ entry }: { entry: PlaylistEntry }) {
  const src = proxyImageUrl(entry.posterUrl);
  return (
    <Group gap="xs" wrap="nowrap">
      <Box
        w={THUMB_WIDTH}
        h={THUMB_HEIGHT}
        style={{
          overflow: "hidden",
          borderRadius: "var(--mantine-radius-sm)",
          background: "var(--mantine-color-default-hover)",
          flexShrink: 0,
        }}
      >
        {src ? (
          <ProxyImage
            src={src}
            alt={entry.number}
            loading="lazy"
            referrerPolicy="no-referrer"
            style={{ display: "block", width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : null}
      </Box>
      <Stack gap={0} style={{ flex: 1, minWidth: 0 }}>
        <Text size="sm" fw={600} truncate="end">
          {entry.number}
        </Text>
        {entry.title && (
          <Text size="xs" c="dimmed" truncate="end">
            {entry.title}
          </Text>
        )}
      </Stack>
    </Group>
  );
}

/**
 * 右侧浮出的待播放队列.
 *
 * 顶部一排是本页的全部队列操作:**播放**交给外部播放器 (启用时) 或内嵌播放器 (未启用时), 语义与列表
 * 里的播放按钮一致; 其余按钮都作用于复选框选中的条目, 全选/反选只改选择, 不动队列本身.
 */
export function PlaylistDrawer({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { t } = useTranslation(["metadata", "common"]);
  const items = usePlaylistStore((state) => state.items);
  const remove = usePlaylistStore((state) => state.remove);
  const move = usePlaylistStore((state) => state.move);
  const localPlayer = useLocalPlayer();
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [queue, setQueue] = useState<readonly PlaylistEntry[]>([]);

  const ids = items.map((item) => item.metadataId);
  const chosenIds = ids.filter((id) => selected.has(id));
  // 没有勾选时, "播放"作用于整份队列: 打开抽屉直接点播放是最常见的用法.
  const targets =
    chosenIds.length > 0 ? items.filter((item) => selected.has(item.metadataId)) : items;

  const openExternal = useAppMutation({
    mutationOptions: openLocalPlaylistMutation(),
    onSuccess: (result) => {
      notifications.show({
        message: t("metadata:playlist.openedExternal", {
          count: result.paths.length,
          player: result.player ?? t("metadata:detail.openLocalSystem"),
        }),
        color: "blue",
      });
    },
  });

  function handlePlay() {
    if (targets.length === 0) return;
    if (localPlayer.data?.enabled === true) {
      openExternal.mutate({ body: { metadata_ids: targets.map((item) => item.metadataId) } });
      return;
    }
    setQueue(targets);
  }

  function toggleOne(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <>
      <Drawer
        opened={opened}
        onClose={onClose}
        position="right"
        size="md"
        title={t("metadata:playlist.title")}
        // 抽屉的 body 默认不撑满: 列表要自己滚, 因此把 body 变成纵向 flex 容器并按高度撑开.
        styles={{ body: { display: "flex", flexDirection: "column", minHeight: 0 } }}
      >
        <Stack gap="xs" style={{ flex: 1, minHeight: 0 }}>
          <Group gap={4} wrap="nowrap">
            <HintedActionIcon
              label={t("metadata:playlist.selectAll")}
              variant="subtle"
              size="sm"
              disabled={ids.length === 0}
              onClick={() => setSelected(new Set(ids))}
            >
              <IconSelectAll size={16} />
            </HintedActionIcon>
            <HintedActionIcon
              label={t("metadata:playlist.invert")}
              variant="subtle"
              size="sm"
              disabled={ids.length === 0}
              onClick={() => setSelected(new Set(ids.filter((id) => !selected.has(id))))}
            >
              <IconSquareOff size={16} />
            </HintedActionIcon>
            <HintedActionIcon
              label={t("metadata:playlist.moveUp")}
              variant="subtle"
              size="sm"
              disabled={chosenIds.length === 0}
              onClick={() => move(chosenIds, -1)}
            >
              <IconArrowUp size={16} />
            </HintedActionIcon>
            <HintedActionIcon
              label={t("metadata:playlist.moveDown")}
              variant="subtle"
              size="sm"
              disabled={chosenIds.length === 0}
              onClick={() => move(chosenIds, 1)}
            >
              <IconArrowDown size={16} />
            </HintedActionIcon>
            <HintedActionIcon
              label={t("metadata:playlist.remove")}
              variant="subtle"
              color="red"
              size="sm"
              disabled={chosenIds.length === 0}
              onClick={() => {
                remove(chosenIds);
                setSelected(new Set());
              }}
            >
              <IconTrash size={16} />
            </HintedActionIcon>
            <Box ml="auto">
              <Tooltip label={t("metadata:playlist.play")}>
                <ActionIcon
                  variant="filled"
                  size="md"
                  loading={openExternal.isPending}
                  disabled={items.length === 0}
                  onClick={handlePlay}
                  aria-label={t("metadata:playlist.play")}
                >
                  <IconPlayerPlay size={16} />
                </ActionIcon>
              </Tooltip>
            </Box>
          </Group>
          <Divider />
          {items.length === 0 ? (
            <Center py="xl">
              <Text size="sm" c="dimmed">
                {t("metadata:playlist.empty")}
              </Text>
            </Center>
          ) : (
            <ScrollArea style={{ flex: 1, minHeight: 0 }} offsetScrollbars>
              <Stack gap="xs" pr="xs">
                {items.map((entry) => (
                  <Group key={entry.metadataId} gap="xs" wrap="nowrap" align="center">
                    <Checkbox
                      checked={selected.has(entry.metadataId)}
                      onChange={() => toggleOne(entry.metadataId)}
                      aria-label={entry.number}
                    />
                    <Box style={{ flex: 1, minWidth: 0 }}>
                      <Row entry={entry} />
                    </Box>
                    <HintedActionIcon
                      label={t("metadata:playlist.remove")}
                      variant="subtle"
                      color="gray"
                      size="sm"
                      onClick={() => remove([entry.metadataId])}
                    >
                      <IconX size={14} />
                    </HintedActionIcon>
                  </Group>
                ))}
              </Stack>
            </ScrollArea>
          )}
        </Stack>
      </Drawer>
      <PlaybackQueueModal
        entries={queue}
        title={queue.length === 1 ? queue[0].number : t("metadata:playlist.title")}
        onClose={() => setQueue([])}
      />
    </>
  );
}
