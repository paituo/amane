import { Group, Slider, Text, Tooltip } from "@mantine/core";
import { IconColumns } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { MAX_GRID_COLUMNS, MIN_GRID_COLUMNS, useGridColumns } from "@/lib/media/grid-columns";
import { useUIStore } from "@/stores/ui";

interface GridColumnsSliderProps {
  /** 控件整体宽度; 标题行与筛选栏的空间不同, 由调用方给. */
  w?: number | string;
}

/**
 * 每行列数滑块.
 *
 * 值是全局偏好 (ui store), 各处网格共用; 滑钮旁的数字是实际生效的列数 —
 * 窄屏被断点收窄时显示的也还是那个数, 用户由此看得到设定与视口的关系.
 */
export function GridColumnsSlider({ w = 132 }: GridColumnsSliderProps) {
  const { t } = useTranslation("metadata");
  const preferred = useUIStore((s) => s.gridColumns);
  const setGridColumns = useUIStore((s) => s.setGridColumns);
  const effective = useGridColumns();

  return (
    <Group gap="xs" wrap="nowrap" w={w} style={{ flexShrink: 0 }}>
      <IconColumns size={16} color="var(--mantine-color-dimmed)" aria-hidden />
      <Tooltip label={t("view.columnsHint", { min: MIN_GRID_COLUMNS, max: MAX_GRID_COLUMNS })}>
        <Text size="xs" c="dimmed" ff="monospace" style={{ flexShrink: 0 }}>
          {effective}
        </Text>
      </Tooltip>
      <Slider
        value={preferred}
        onChange={setGridColumns}
        min={MIN_GRID_COLUMNS}
        max={MAX_GRID_COLUMNS}
        step={1}
        size="sm"
        flex={1}
        miw={64}
        aria-label={t("view.columnsPerRow")}
      />
    </Group>
  );
}
