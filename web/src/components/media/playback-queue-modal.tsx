import { ActionIcon, Box, Center, Group, Modal, Text } from "@mantine/core";
import { IconPlayerSkipBack, IconPlayerSkipForward } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { PlaybackPanel } from "@/components/media/playback-panel";
import type { PlaylistEntry } from "@/stores/playlist";

/**
 * 内嵌播放器的队列: 把一批条目按顺序播下去.
 *
 * 未启用外部播放器时, 播放列表与"播放选中项"都落到这里. 面板每次换条目都重新挂载 (`key`), 因为它的
 * 来源/码流选择、失败记录都按单个条目缓存, 复用会把上一部片子的选择带到下一部.
 */
export function PlaybackQueueModal({
  entries,
  title,
  onClose,
}: {
  /** 队列内容; 空数组表示关闭. */
  entries: readonly PlaylistEntry[];
  title?: string;
  onClose: () => void;
}) {
  const { t } = useTranslation("metadata");
  const opened = entries.length > 0;
  // 队列换了内容就回到第一项. 用渲染期调整而不是 effect: React 会在本次渲染内立刻重渲染, 不会先闪一帧
  // 旧进度; effect 里 setState 会多一次提交, 也是 lint 明令禁止的写法.
  const queueKey = entries.map((entry) => entry.metadataId).join(",");
  const [position, setPosition] = useState({ key: queueKey, index: 0 });
  if (position.key !== queueKey) setPosition({ key: queueKey, index: 0 });
  const index = position.index;
  const setIndex = (next: (value: number) => number) =>
    setPosition((prev) => ({ ...prev, index: next(prev.index) }));
  // 队列可能在打开期间被改动 (比如在抽屉里删掉正在播的那一项), 越界时退回最后一项.
  const current = entries[Math.min(index, Math.max(entries.length - 1, 0))];
  const hasPrev = index > 0;
  const hasNext = index < entries.length - 1;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={title ?? current?.number ?? ""}
      size="90rem"
      centered
    >
      {current != null && (
        <Box>
          <Group justify="space-between" mb="xs" wrap="nowrap">
            <ActionIcon
              variant="default"
              size="lg"
              disabled={!hasPrev}
              onClick={() => setIndex((value) => Math.max(value - 1, 0))}
              aria-label={t("playlist.prev")}
            >
              <IconPlayerSkipBack size={18} />
            </ActionIcon>
            <Center style={{ flex: 1 }}>
              <Text size="sm" c="dimmed">
                {t("playlist.position", { index: index + 1, total: entries.length })}
              </Text>
            </Center>
            <ActionIcon
              variant="default"
              size="lg"
              disabled={!hasNext}
              onClick={() => setIndex((value) => Math.min(value + 1, entries.length - 1))}
              aria-label={t("playlist.next")}
            >
              <IconPlayerSkipForward size={18} />
            </ActionIcon>
          </Group>
          <PlaybackPanel
            key={current.metadataId}
            metadataId={current.metadataId}
            playNonce={1}
            seekRequest={null}
            onSeekHandled={() => {}}
            onCanSeekChange={() => {}}
          />
        </Box>
      )}
    </Modal>
  );
}
