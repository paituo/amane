import { Modal } from "@mantine/core";
import { PlaybackPanel } from "./playback-panel";

/**
 * 内嵌播放弹窗: 未启用外部播放器时, 海报墙与片库列表的播放按钮都打开这里.
 *
 * 打开即自动播放 (`playNonce=1`); 面板自己按条目探测来源与码流, 两个宿主只上报 id.
 */
export function PlaybackModal({
  metadataId,
  title,
  onClose,
}: {
  /** 待播放的条目; null 表示关闭. */
  metadataId: number | null;
  title?: string;
  onClose: () => void;
}) {
  return (
    <Modal opened={metadataId != null} onClose={onClose} title={title ?? ""} size="90rem" centered>
      {metadataId != null && (
        <PlaybackPanel
          metadataId={metadataId}
          playNonce={1}
          seekRequest={null}
          onSeekHandled={() => {}}
          onCanSeekChange={() => {}}
        />
      )}
    </Modal>
  );
}
