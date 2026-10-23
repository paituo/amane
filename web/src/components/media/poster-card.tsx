import { AspectRatio, Box, Card, Center, Group, Text } from "@mantine/core";
import {
  IconBan,
  IconCheck,
  IconInfoCircle,
  IconPlayerPlayFilled,
  IconStar,
  IconUser,
} from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import type { MetadataResponse } from "@/client/types.gen";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { FilePhaseBadges } from "@/components/media/file-phase-badges";
import { OverlayChip, OverlayChipLabel } from "@/components/media/overlay-chip";
import { ProxyImage } from "@/components/media/proxy-image";
import { VrOverlayChip } from "@/components/media/vr-badge";
import { ageAtRelease } from "@/lib/format-birthday";
import { formatReleaseYearMonth } from "@/lib/format-release";
import { useFavoriteIds, useFavoriteMutation } from "@/lib/media/favorites";
import { useBlacklistIds, useBlacklistMutation } from "@/lib/media/blacklist";
import { proxyImageUrl } from "@/lib/utils";
import classes from "./poster-card.module.css";

interface PosterCardProps {
  item: MetadataResponse;
  /** 传入时按影片发行日标注演员当时年龄 (演员详情出演墙). */
  actorBirthday?: string | null;
  /** 传入时渲染左上角复选框, 选中态由宿主网格持有. */
  selected?: boolean;
  onToggleSelect?: () => void;
  /** 传入时渲染播放按钮, 播放弹窗由宿主网格统一打开. */
  onPlay?: () => void;
  /**
   * 传入时点击就地打开详情弹窗; 修饰键与中键仍按链接处理 (新标签打开详情页面),
   * 因此只在普通左键时接管事件.
   */
  onOpen?: () => void;
}

/** 卡片整体是链接, 覆盖层内的交互一律阻断导航. */
function stopLink(event: MouseEvent) {
  event.preventDefault();
  event.stopPropagation();
}

/**
 * 海报卡片 (参照 media-player1): 海报铺满整卡, 番号/标题/年份叠在图片底部;
 * 悬停时卡片上浮、画面微放大, 浮现播放/详情/收藏/黑名单四个操作图标.
 */
export function PosterCard({
  item,
  actorBirthday,
  selected,
  onToggleSelect,
  onPlay,
  onOpen,
}: PosterCardProps) {
  const { t } = useTranslation("metadata");
  const [errored, setErrored] = useState(false);
  const favoriteIds = useFavoriteIds();
  const favoriteMutation = useFavoriteMutation();
  const isFavorite = favoriteIds.has(item.id);
  const blacklistIds = useBlacklistIds();
  const blacklistMutation = useBlacklistMutation();
  const isBlacklisted = blacklistIds.has(item.id);
  const posterUrl = proxyImageUrl(item.poster_url ?? item.poster_urls?.[0]);
  const showImage = posterUrl && !errored;
  const releaseLabel = formatReleaseYearMonth(item.release);
  const ageThen = ageAtRelease(actorBirthday, item.release);

  return (
    <Link
      to="/meta/$metadataId"
      params={{ metadataId: String(item.id) }}
      style={{ textDecoration: "none", color: "inherit", display: "block" }}
      onClick={(event) => {
        if (onOpen == null) return;
        // 保留浏览器行为: 中键与修饰键由链接自己打开详情页面.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (event.button !== 0) return;
        event.preventDefault();
        onOpen();
      }}
    >
      <Card
        padding={0}
        radius="md"
        withBorder
        className={classes.card}
        style={selected ? { borderColor: "var(--mantine-color-blue-6)" } : undefined}
      >
        <Box pos="relative">
          <AspectRatio ratio={0.7}>
            <Box className={classes.zoom} h="100%">
              {showImage ? (
                <ProxyImage
                  src={posterUrl}
                  alt={item.number}
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  style={{
                    display: "block",
                    width: "100%",
                    height: "100%",
                    objectFit: "cover",
                  }}
                  onError={() => setErrored(true)}
                />
              ) : (
                <Center bg="var(--mantine-color-default-hover)" h="100%">
                  <Text size="sm" ff="monospace" c="dimmed" ta="center" px="xs">
                    {item.number}
                  </Text>
                </Center>
              )}
            </Box>
          </AspectRatio>
          <Box className={classes.gradient} />
          {onToggleSelect != null && (
            <button
              type="button"
              className={`${classes.checkbox} ${selected ? classes.checkboxOn : ""}`}
              title={t("card.select")}
              aria-pressed={selected}
              onClick={(event) => {
                stopLink(event);
                onToggleSelect();
              }}
            >
              {selected && <IconCheck size={14} stroke={3} />}
            </button>
          )}
          {/* 与评分同角成组: 左上角被复选框占用, 单独放会与它重叠. */}
          {(item.vr || item.score != null) && (
            <Group pos="absolute" top={6} right={6} gap={4} wrap="nowrap">
              {item.vr && <VrOverlayChip />}
              {item.score != null && (
                <OverlayChip>
                  <IconStar
                    size={11}
                    color="var(--mantine-color-yellow-4)"
                    fill="var(--mantine-color-yellow-4)"
                  />
                  <OverlayChipLabel>{item.score.toFixed(1)}</OverlayChipLabel>
                </OverlayChip>
              )}
            </Group>
          )}
          <Box className={classes.info}>
            <Group className={classes.actions} gap={6} wrap="nowrap">
              {onPlay != null && (
                <HintedActionIcon
                  size="sm"
                  variant="transparent"
                  color="white"
                  style={{ background: "rgba(0, 0, 0, 0.45)" }}
                  label={t("card.play")}
                  onClick={(event) => {
                    stopLink(event);
                    onPlay();
                  }}
                >
                  <IconPlayerPlayFilled size={14} />
                </HintedActionIcon>
              )}
              {onOpen != null && (
                <HintedActionIcon
                  size="sm"
                  variant="transparent"
                  color="white"
                  style={{ background: "rgba(0, 0, 0, 0.45)" }}
                  label={t("card.detail")}
                  onClick={(event) => {
                    stopLink(event);
                    onOpen();
                  }}
                >
                  <IconInfoCircle size={14} />
                </HintedActionIcon>
              )}
              <HintedActionIcon
                size="sm"
                variant="transparent"
                color={isFavorite ? "yellow" : "white"}
                style={{ background: "rgba(0, 0, 0, 0.45)" }}
                loading={
                  favoriteMutation.isPending && favoriteMutation.variables?.metadataId === item.id
                }
                aria-pressed={isFavorite}
                label={isFavorite ? t("favorite.remove") : t("favorite.add")}
                onClick={(event) => {
                  stopLink(event);
                  favoriteMutation.mutate({ metadataId: item.id, favorite: !isFavorite });
                }}
              >
                {isFavorite ? <IconStar size={14} fill="currentColor" /> : <IconStar size={14} />}
              </HintedActionIcon>
              <HintedActionIcon
                size="sm"
                variant="transparent"
                color={isBlacklisted ? "red" : "white"}
                style={{ background: "rgba(0, 0, 0, 0.45)" }}
                loading={
                  blacklistMutation.isPending && blacklistMutation.variables?.metadataId === item.id
                }
                aria-pressed={isBlacklisted}
                label={isBlacklisted ? t("blacklist.remove") : t("blacklist.add")}
                onClick={(event) => {
                  stopLink(event);
                  blacklistMutation.mutate({
                    metadataId: item.id,
                    blacklisted: !isBlacklisted,
                  });
                }}
              >
                <IconBan size={14} />
              </HintedActionIcon>
            </Group>
            <Text
              size="sm"
              fw={700}
              ff="monospace"
              c="var(--mantine-color-blue-4)"
              lineClamp={1}
              title={item.number}
            >
              {item.number}
            </Text>
            {item.title && (
              <Text size="xs" c="white" lineClamp={1} title={item.title}>
                {item.title}
              </Text>
            )}
            {(releaseLabel || ageThen != null || item.file_phase != null) && (
              <Group className={classes.metaRow} gap={4} wrap="nowrap">
                {releaseLabel && (
                  <Text size="xs" c="dimmed" ff="monospace" lts={0.3} style={{ flexShrink: 0 }}>
                    {releaseLabel}
                  </Text>
                )}
                <Box style={{ minWidth: 0, overflow: "hidden" }}>
                  <FilePhaseBadges phase={item.file_phase} />
                </Box>
                {ageThen != null && (
                  <OverlayChip>
                    <IconUser size={12} color="var(--mantine-color-grape-3)" />
                    <OverlayChipLabel>{t("actors.ageShort", { age: ageThen })}</OverlayChipLabel>
                  </OverlayChip>
                )}
              </Group>
            )}
          </Box>
        </Box>
      </Card>
    </Link>
  );
}
