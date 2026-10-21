import { AspectRatio, Center, SimpleGrid, Skeleton, Stack, Text } from "@mantine/core";
import type { MetadataResponse } from "@/client/types.gen";
import { MetadataViewer } from "@/components/media/metadata-viewer";
import { useGridColumns } from "@/lib/media/grid-columns";
import { useCallback, useState } from "react";
import { PosterCard } from "./poster-card";

interface PosterGridProps {
  items: MetadataResponse[];
  loading?: boolean;
  emptyMessage?: string;
  /** 传入时海报显示演员出演时年龄. */
  actorBirthday?: string | null;
  /** 还有下一页时由弹窗在接近末尾处调用, 继续往下翻. */
  onFetchMore?: () => void;
}

/**
 * 海报墙 + 就地详情.
 *
 * 点击卡片不是导航而是原地打开弹窗 (方向键与两侧箭头翻阅已加载的序列), 但链接仍旧保留:
 * 中键与修饰键沿用浏览器的新标签行为, 那种方式打开的是详情页面.
 */
export function PosterGrid({
  items,
  loading = false,
  emptyMessage,
  actorBirthday,
  onFetchMore,
}: PosterGridProps) {
  const cols = useGridColumns();
  // 按 id 记录打开的那一部: 追加分页或重新获取之后, 位置仍然对得上.
  const [openId, setOpenId] = useState<number | null>(null);

  const open = useCallback((id: number) => setOpenId(id), []);

  if (loading) {
    return (
      <SimpleGrid cols={cols} spacing="md">
        {Array.from({ length: 12 }, (_, i) => (
          <Stack key={i} gap={2}>
            <AspectRatio ratio={0.7}>
              <Skeleton radius="md" />
            </AspectRatio>
            <Skeleton h={12} w="70%" />
          </Stack>
        ))}
      </SimpleGrid>
    );
  }

  if (items.length === 0) {
    return (
      <Center py="xl">
        <Text c="dimmed" size="sm">
          {emptyMessage}
        </Text>
      </Center>
    );
  }

  return (
    <>
      <SimpleGrid cols={cols} spacing="md">
        {items.map((item) => (
          <PosterCard
            key={item.id}
            item={item}
            actorBirthday={actorBirthday}
            onOpen={() => open(item.id)}
          />
        ))}
      </SimpleGrid>
      <MetadataViewer
        items={items}
        openId={openId}
        onOpenIdChange={setOpenId}
        onFetchMore={onFetchMore}
      />
    </>
  );
}
