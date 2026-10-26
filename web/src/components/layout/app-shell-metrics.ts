/** `AppShell.Header` 的高度, 传给 AppShell; 页面不得另行书写该数值. */
export const APP_SHELL_HEADER_HEIGHT = 60;

/**
 * AppShell.Main 内容区高度: 视口高度扣除 header 与上下 padding.
 * 两个扣除项均取自 AppShell 写入 `:root` 的变量: 该变量以 rem 计算, 浏览器默认字号改变时仍与实际 header 同高,
 * 手写像素值会让钉高页面超出 Main 内容区并被裁掉.
 */
export const APP_SHELL_MAIN_HEIGHT =
  "calc(var(--amane-vh) - var(--app-shell-header-height) - 2 * var(--app-shell-padding))" as const;

/**
 * 浏览页壳吸附标头的实测高度 (CSS 变量名). 由 `BrowsePageShell` 用 ResizeObserver 量出后写在根节点上,
 * 后代据此把自己叠在标头正下方 - 标头高度随高级筛选面板展开/窄屏折行变化, 不能写死像素.
 */
export const BROWSE_HEADER_HEIGHT_VAR = "--amane-browse-header-h";

/**
 * 次级吸附条的 `top`: AppShell 顶栏 + 浏览页壳标头.
 * 不在浏览页壳内的页面 (演员/分类详情) 上该变量缺省, 退化为只吸附在 AppShell 顶栏下沿.
 */
export const STICKY_UNDER_BROWSE_HEADER_TOP =
  `calc(${APP_SHELL_HEADER_HEIGHT}px + var(${BROWSE_HEADER_HEIGHT_VAR}, 0px))` as const;
