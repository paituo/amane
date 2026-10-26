import { ActionIcon, Box, Group, Modal } from "@mantine/core";
import { useHotkeys } from "@mantine/hooks";
import { IconChevronLeft, IconChevronRight } from "@tabler/icons-react";
import type { MouseEvent } from "react";
import { useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import type { MetadataResponse } from "@/client/types.gen";
import { useNarrowViewport } from "@/hooks/use-narrow-viewport";
import { MetadataDetail } from "./metadata-detail";

/** 距末尾还剩这么多张时追加下一页: 用户翻到当前最后一张之前, 后续条目已经接上. */
const PREFETCH_REMAINING = 2;

/** 弹窗内容区的最大高度. 左右箭头固定在内容之外, 因此内容区自身滚动; 内容不足时按内容收缩. */
const VIEWER_HEIGHT = "calc(var(--amane-vh) * 0.86)";

interface MetadataViewerProps {
  /** 可翻阅的序列 — 就是所在网格当前已加载的条目. */
  items: MetadataResponse[];
  /** 打开的条目; null 为关闭. 按 id 而不是下标, 追加分页后翻开的那张仍然对得上. */
  openId: number | null;
  onOpenIdChange: (id: number | null) => void;
  /** 还有下一页时在接近末尾处调用, 由列表把后续条目接上. */
  onFetchMore?: () => void;
}

/**
 * 就地查看影片详情: 居中弹窗, 点窗口外关闭, 两侧箭头与左右方向键翻上一篇 / 下一篇.
 *
 * 序列是宿主网格已加载的那一段, 翻到尽头会停在最后一张 (不环绕),
 * 无限滚动在此基础上再补一批, 因此左右切换与「已经加载了多少」始终一致.
 */
export function MetadataViewer({
  items,
  openId,
  onOpenIdChange,
  onFetchMore,
}: MetadataViewerProps) {
  const { t } = useTranslation("common");
  const narrowViewport = useNarrowViewport("md");
  const scrollRef = useRef<HTMLDivElement>(null);
  /** 上一次反映在滚动位置上的条目, 用于区分"换了一部"与"同一部重渲染". */
  const lastOpenIdRef = useRef<number | null>(openId);

  // 条目可能已被筛选或删除挤出列表, 取不到下标即视为关闭.
  const index = openId == null ? -1 : items.findIndex((item) => item.id === openId);
  const opened = index >= 0;
  const current = opened ? items[index] : null;

  const step = useCallback(
    (delta: number) => {
      if (index < 0) return;
      const target = index + delta;
      if (target < 0 || target > items.length - 1) return;
      if (onFetchMore && items.length - 1 - target <= PREFETCH_REMAINING) onFetchMore();
      onOpenIdChange(items[target].id);
    },
    [index, items, onFetchMore, onOpenIdChange],
  );

  useHotkeys([
    ["ArrowLeft", () => step(-1)],
    ["ArrowRight", () => step(1)],
  ]);

  // 换一部片子就从顶部开始查看: 详情很长, 沿用上一篇的滚动位置等于打开的同时已滚到一半.
  // 与上一次的条目比较而不是无条件复位, 同一部片子内部的重渲染不该跳到顶部.
  useEffect(() => {
    if (lastOpenIdRef.current === openId) return;
    lastOpenIdRef.current = openId;
    const el = scrollRef.current;
    if (el) el.scrollTop = 0;
  }, [openId]);

  /** 详情里的跳转会把人带去别的工作面, 弹窗不该继续盖在上面. */
  function handleContentClick(event: MouseEvent<HTMLDivElement>) {
    const link = (event.target as HTMLElement | null)?.closest("a[href]");
    if (link == null) return;
    if ((link as HTMLAnchorElement).target === "_blank") return;
    onOpenIdChange(null);
  }

  const prevButton = (aside: boolean) => (
    <ActionIcon
      variant={aside ? "default" : "subtle"}
      size="lg"
      radius="xl"
      disabled={index <= 0}
      onClick={() => step(-1)}
      aria-label={t("actions.previous")}
      {...(aside
        ? {
            pos: "absolute",
            left: 4,
            top: "50%",
            style: { transform: "translateY(-50%)", zIndex: 2 },
          }
        : {})}
    >
      <IconChevronLeft size={22} />
    </ActionIcon>
  );

  const nextButton = (aside: boolean) => (
    <ActionIcon
      variant={aside ? "default" : "subtle"}
      size="lg"
      radius="xl"
      disabled={index < 0 || index >= items.length - 1}
      onClick={() => step(1)}
      aria-label={t("actions.next")}
      {...(aside
        ? {
            pos: "absolute",
            right: 4,
            top: "50%",
            style: { transform: "translateY(-50%)", zIndex: 2 },
          }
        : {})}
    >
      <IconChevronRight size={22} />
    </ActionIcon>
  );

  return (
    <Modal
      opened={opened}
      onClose={() => onOpenIdChange(null)}
      centered
      size={narrowViewport ? "100%" : "74rem"}
      padding="md"
      withCloseButton={false}
      // 翻阅位置与关闭按钮都收进详情标题行, 弹窗自身不再留标题栏.
      styles={{ header: { display: "none" }, body: { paddingBlock: "var(--mantine-spacing-xs)" } }}
    >
      {current && (
        <Box pos="relative" style={{ maxHeight: VIEWER_HEIGHT }} onClick={handleContentClick}>
          {/* 宽屏为箭头留出两侧的空间: 内容再长也不会滚到箭头之下. */}
          <Box
            ref={scrollRef}
            px={narrowViewport ? 0 : "xl"}
            style={{ maxHeight: VIEWER_HEIGHT, overflowY: "auto" }}
          >
            {narrowViewport && (
              <Group justify="space-between" pb="xs">
                {prevButton(false)}
                {nextButton(false)}
              </Group>
            )}
            <MetadataDetail
              key={current.id}
              metadataId={String(current.id)}
              embedded
              counter={`${index + 1} / ${items.length}`}
              onClose={() => onOpenIdChange(null)}
              onDeleted={() => onOpenIdChange(null)}
            />
          </Box>
          {narrowViewport ? null : (
            <>
              {prevButton(true)}
              {nextButton(true)}
            </>
          )}
        </Box>
      )}
    </Modal>
  );
}
