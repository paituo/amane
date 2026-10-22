import { AspectRatio, Box, Card, Center, Group, Stack, Text } from "@mantine/core";
import { IconCalendar, IconStar, IconUser } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { MetadataResponse } from "@/client/types.gen";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { FilePhaseOverlay } from "@/components/media/file-phase-badges";
import { OverlayChip, OverlayChipLabel } from "@/components/media/overlay-chip";
import { ProxyImage } from "@/components/media/proxy-image";
import { VrOverlayChip } from "@/components/media/vr-badge";
import { ageAtRelease } from "@/lib/format-birthday";
import { formatReleaseYearMonth } from "@/lib/format-release";
import { useFavoriteIds, useFavoriteMutation } from "@/lib/media/favorites";
import { proxyImageUrl } from "@/lib/utils";

interface PosterCardProps {
  item: MetadataResponse;
  /** 传入时按影片发行日标注演员当时年龄 (演员详情出演墙). */
  actorBirthday?: string | null;
  /**
   * 传入时点击就地打开详情弹窗; 修饰键与中键仍按链接处理 (新标签打开详情页面),
   * 因此只在普通左键时接管事件.
   */
  onOpen?: () => void;
}

/** 加载失败退化为番号占位块. */
export function PosterCard({ item, actorBirthday, onOpen }: PosterCardProps) {
  const { t } = useTranslation("metadata");
  const [errored, setErrored] = useState(false);
  const favoriteIds = useFavoriteIds();
  const favoriteMutation = useFavoriteMutation();
  const isFavorite = favoriteIds.has(item.id);
  const posterUrl = proxyImageUrl(item.poster_url ?? item.poster_urls?.[0]);
  const showImage = posterUrl && !errored;
  const actors = item.actors ?? [];
  const actorsLabel = actors.length > 0 ? actors.join(" / ") : null;
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
      <Card padding={0} radius="md" withBorder style={{ overflow: "hidden" }}>
        <Box pos="relative">
          <AspectRatio ratio={0.7}>
            {showImage ? (
              <ProxyImage
                src={posterUrl}
                alt={item.number}
                loading="lazy"
                referrerPolicy="no-referrer"
                style={{ display: "block" }}
                onError={() => setErrored(true)}
              />
            ) : (
              <Center bg="var(--mantine-color-default-hover)" h="100%">
                <Text size="sm" ff="monospace" c="dimmed" ta="center" px="xs">
                  {item.number}
                </Text>
              </Center>
            )}
          </AspectRatio>
          <FilePhaseOverlay
            phase={item.file_phase}
            bottomExtra={
              ageThen != null ? (
                <OverlayChip>
                  <IconUser size={12} color="var(--mantine-color-grape-3)" />
                  <OverlayChipLabel>{t("actors.ageShort", { age: ageThen })}</OverlayChipLabel>
                </OverlayChip>
              ) : undefined
            }
          />
          {/* 与评分同角成组: 左上角被相位水印 (马赛克标记) 占用, 单独放会与它重叠. */}
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
          {releaseLabel && (
            <Box pos="absolute" bottom={6} right={6}>
              <OverlayChip>
                <IconCalendar size={12} color="var(--mantine-color-cyan-3)" />
                <OverlayChipLabel ff="monospace" lts={0.3}>
                  {releaseLabel}
                </OverlayChipLabel>
              </OverlayChip>
            </Box>
          )}
        </Box>
        <Stack gap={4} p="xs">
          {/* 四个角都被水印与评分占满, 收藏动作落在标题行末尾, 不遮挡海报画面. */}
          <Group gap={4} wrap="nowrap" align="center">
            <Text size="sm" fw={600} ff="monospace" lineClamp={1} style={{ minWidth: 0, flex: 1 }}>
              {item.number}
            </Text>
            <HintedActionIcon
              size="sm"
              variant="subtle"
              color={isFavorite ? "yellow" : "gray"}
              loading={
                favoriteMutation.isPending && favoriteMutation.variables?.metadataId === item.id
              }
              aria-pressed={isFavorite}
              label={isFavorite ? t("favorite.remove") : t("favorite.add")}
              onClick={(event) => {
                // 整张卡片是链接, 收藏不能顺带导航.
                event.preventDefault();
                event.stopPropagation();
                favoriteMutation.mutate({ metadataId: item.id, favorite: !isFavorite });
              }}
            >
              {isFavorite ? <IconStar size={14} fill="currentColor" /> : <IconStar size={14} />}
            </HintedActionIcon>
          </Group>
          {item.title && (
            <Text size="xs" c="dimmed" lineClamp={2} title={item.title}>
              {item.title}
            </Text>
          )}
          {actorsLabel && (
            <Group gap={5} wrap="nowrap" align="center" title={actorsLabel}>
              <Box
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  width: 18,
                  height: 18,
                  borderRadius: "var(--mantine-radius-sm)",
                  background: "var(--mantine-color-grape-filled)",
                  color: "var(--mantine-color-white)",
                }}
              >
                <IconUser size={11} stroke={2.2} />
              </Box>
              <Text
                size="xs"
                fw={600}
                lineClamp={1}
                style={{
                  minWidth: 0,
                  flex: 1,
                  color: "light-dark(var(--mantine-color-grape-7), var(--mantine-color-grape-3))",
                }}
              >
                {actorsLabel}
              </Text>
            </Group>
          )}
        </Stack>
      </Card>
    </Link>
  );
}
