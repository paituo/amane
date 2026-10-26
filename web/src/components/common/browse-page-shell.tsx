import { ActionIcon, Box, Drawer, Group, Stack } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconAdjustmentsHorizontal } from "@tabler/icons-react";
import { type CSSProperties, type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  APP_SHELL_HEADER_HEIGHT,
  APP_SHELL_MAIN_HEIGHT,
  BROWSE_HEADER_HEIGHT_VAR,
} from "@/components/layout/app-shell-metrics";
import { useNarrowViewport } from "@/hooks/use-narrow-viewport";

export interface BrowsePageShellProps {
  /** 标题区 (可含面包屑). */
  title: ReactNode;
  /**
   * 视图切换 (海报/列表, 词云/列表 等).
   * 与标题同一行居中, 作为全局模式总览.
   */
  viewSwitch?: ReactNode;
  /** 标题行右侧可选动作 (少用). */
  actions?: ReactNode;
  /** 搜索行最左侧: 总计数等摘要 (不参与居中组; 小屏移到搜索上方). */
  summary?: ReactNode;
  /** 搜索框 - 与 extras / pageSize 组成居中控件组. */
  search: ReactNode;
  /** 搜索右侧附属控件 (排序, 筛选). */
  extras?: ReactNode;
  /** 搜索右侧末尾 (分页大小). */
  pageSize?: ReactNode;
  /**
   * 高级筛选面板 (通常是受控 `Collapse`).
   * 宽屏按原样渲染在 children 之前; 窄屏进底部面板, 与它的开关同处一地.
   */
  filterPanel?: ReactNode;
  /**
   * 撑满 AppShell.Main 剩余高度, children 吃掉标题/搜索之下的空间.
   * 给 list + ListToolbar; 不要给依赖 window 滚动的 grid/cloud.
   */
  fill?: boolean;
  children: ReactNode;
}

/**
 * 片库 / 分类共用的浏览页壳.
 *
 * 宽屏标头层级:
 * 1. 标题 | **居中视图切换** | 右侧动作
 * 2. 左侧摘要 | **(搜索+排序/筛选/每页条数) 整体居中**
 *
 * 窄屏只剩一行 (标题 + 视图切换 + 筛选入口), 其余控件全进底部面板 (见 docs/dev/frontend.md).
 * 因此调用方必须把高级筛选面板经 `filterPanel` 传进来, 不能自行放进 children; 同一批控件只渲染一处.
 *
 * 搜索宽度为 min(480px, 可用宽), 控件组 max-width:100% + min-width:0, 避免小屏横向滚动.
 * fill 时高度钉在 Main 内容区, 标题/搜索不滚, children 必须自己消化剩余高度.
 */
export function BrowsePageShell({
  title,
  viewSwitch,
  actions,
  summary,
  search,
  extras,
  pageSize,
  filterPanel,
  fill = false,
  children,
}: BrowsePageShellProps) {
  const { t } = useTranslation("common");
  const narrowViewport = useNarrowViewport("md");
  const [filtersOpened, { open: openFilters, close: closeFilters }] = useDisclosure(false);

  // 吸附标头的实测高度: 下方的次级吸附条 (网格视图的批量工具条) 靠它叠在标头正下方.
  // 不能写死像素 - 高级筛选面板展开、窄屏折行都会改变标头高度.
  const headerRef = useRef<HTMLDivElement>(null);
  const [headerHeight, setHeaderHeight] = useState(0);

  // 用 layout effect: 首帧前就量好, 否则宽屏布局闪一下再切窄屏时, 次级吸附条会先按旧高度定位.
  useLayoutEffect(() => {
    if (fill) return;
    const el = headerRef.current;
    if (el == null) return;
    const sync = () => setHeaderHeight(el.offsetHeight);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    return () => observer.disconnect();
  }, [fill]);

  return (
    <Stack
      gap="md"
      style={{
        minWidth: 0,
        // fill: 高度钉在 Main 内容区. 滚动交给内容区自己, 标头因此不随列表滚走.
        ...(fill
          ? { height: APP_SHELL_MAIN_HEIGHT, overflow: "hidden" }
          : ({
              // 供后代 (如网格视图的批量工具条) 计算各自吸附位置; 见 BROWSE_HEADER_HEIGHT_VAR.
              [BROWSE_HEADER_HEIGHT_VAR]: `${headerHeight}px`,
            } as CSSProperties)),
      }}
    >
      <Box
        ref={headerRef}
        pb="md"
        style={{
          borderBottom: "1px solid var(--mantine-color-default-border)",
          minWidth: 0,
          flexShrink: 0,
          // 非 fill (海报 / 词云, 靠 window 滚动) 时标头吸附在 AppShell 顶栏下沿;
          // 必须有背景色, 否则列表会从它下面透出来.
          ...(fill
            ? undefined
            : {
                position: "sticky",
                top: APP_SHELL_HEADER_HEIGHT,
                // 层级: 标头 (4) > 次级吸附条 (3, 见 poster-grid) > 卡片悬浮层 (2). 三者必须拉开,
                // 否则海报卡片上 z-index:2 的信息层会盖住吸附条.
                zIndex: 4,
                backgroundColor: "var(--mantine-color-body)",
              }),
        }}
      >
        <Stack gap="md" style={{ minWidth: 0 }}>
          {/* 三列标题行需要 md: sm 断点上导航栏刚展开, 内容宽度反而收窄. */}
          <Box
            visibleFrom="md"
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(0, 1fr) auto minmax(0, 1fr)",
              gap: "var(--mantine-spacing-sm)",
              alignItems: "center",
              minWidth: 0,
            }}
          >
            <Box style={{ minWidth: 0 }}>{title}</Box>
            <Box style={{ minWidth: 0 }}>{viewSwitch}</Box>
            <Group justify="flex-end" gap="sm" wrap="wrap">
              {actions}
            </Group>
          </Box>

          <Group hiddenFrom="md" gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
            <Box style={{ flex: 1, minWidth: 0 }}>{title}</Box>
            {viewSwitch != null && <Box style={{ minWidth: 0 }}>{viewSwitch}</Box>}
            <ActionIcon
              variant="light"
              size="lg"
              aria-label={t("actions.filters")}
              onClick={openFilters}
            >
              <IconAdjustmentsHorizontal size={18} />
            </ActionIcon>
          </Group>

          {narrowViewport ? null : (
            <Stack gap="md" style={{ minWidth: 0 }}>
              {/* 居中控件组在 lg 以下占满整行, 总数只能单独成行, 否则与搜索框重叠 */}
              {summary != null && <Box hiddenFrom="lg">{summary}</Box>}

              <Box pos="relative" w="100%" style={{ minWidth: 0 }}>
                {summary != null && (
                  <Box
                    visibleFrom="lg"
                    style={{
                      position: "absolute",
                      left: 0,
                      top: "50%",
                      transform: "translateY(-50%)",
                      zIndex: 1,
                    }}
                  >
                    {summary}
                  </Box>
                )}
                <Group
                  justify="center"
                  align="center"
                  gap="xs"
                  wrap="wrap"
                  w="100%"
                  style={{ minWidth: 0 }}
                >
                  <Box
                    style={{
                      flex: "1 1 12rem",
                      maxWidth: 480,
                      minWidth: 0,
                      width: "100%",
                    }}
                  >
                    {search}
                  </Box>
                  {extras}
                  {pageSize}
                </Group>
              </Box>
            </Stack>
          )}

          {/* 高级筛选与搜索同属标头: 随标头一起固定, 不跟着列表滚走. */}
          {narrowViewport ? null : filterPanel}
        </Stack>
      </Box>

      <Drawer
        opened={filtersOpened && narrowViewport}
        onClose={closeFilters}
        position="bottom"
        size="60%"
        title={t("actions.filters")}
      >
        <Stack gap="md" style={{ minWidth: 0 }}>
          {summary}
          <Box style={{ minWidth: 0 }}>{search}</Box>
          {filterPanel}
          {extras}
          {pageSize}
          {actions != null && (
            <Group justify="flex-end" gap="sm" wrap="wrap">
              {actions}
            </Group>
          )}
        </Stack>
      </Drawer>

      <Stack
        gap="md"
        style={{
          minWidth: 0,
          ...(fill ? { flex: 1, minHeight: 0, overflowY: "auto", overflowX: "hidden" } : undefined),
        }}
      >
        {children}
      </Stack>
    </Stack>
  );
}
