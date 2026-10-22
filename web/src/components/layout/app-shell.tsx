import {
  ActionIcon,
  AppShell,
  Box,
  Burger,
  Divider,
  Group,
  Loader,
  Menu,
  NavLink,
  ScrollArea,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import {
  IconBrandGithub,
  IconBookmark,
  IconCategory,
  IconClock,
  IconDeviceMobile,
  IconFileText,
  IconFolders,
  IconLanguage,
  IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand,
  IconListDetails,
  IconListTree,
  IconMessageChatbot,
  IconMoonStars,
  IconMovie,
  IconPuzzle,
  IconRss,
  IconSearch,
  IconSettings,
  IconStar,
  IconSun,
  IconSunMoon,
  IconUsers,
  IconWorldSearch,
  type Icon,
} from "@tabler/icons-react";
import { useMutation } from "@tanstack/react-query";
import { Link, Outlet, useLocation, useNavigate } from "@tanstack/react-router";
import type { ParseKeys } from "i18next";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { APP_SHELL_HEADER_HEIGHT } from "@/components/layout/app-shell-metrics";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";
import { VersionMenu } from "@/components/layout/version-menu";
import { APP_NAME, GITHUB_URL } from "@/lib/app";
import { ensureFavoriteTagId, useFavoriteTagId } from "@/lib/media/favorites";
import { navItemClick } from "@/lib/nav-defaults";
import { shellEnvironment } from "@/lib/shell";
import { useConnectionStore } from "@/stores/connection";
import { useUIStore } from "@/stores/ui";

type CommonKey = ParseKeys<"common">;

interface NavItem {
  to: string;
  labelKey: CommonKey;
  icon: Icon;
  end?: boolean;
  /** 收藏入口: 与片库同路径, 落地为带收藏标签参数的片库视图. */
  favorites?: boolean;
}

interface NavGroup {
  key: string;
  labelKey: CommonKey;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    key: "browse",
    labelKey: "nav.groups.browse",
    items: [
      { to: "/", labelKey: "nav.agent", icon: IconMessageChatbot },
      { to: "/meta", labelKey: "nav.meta", icon: IconMovie },
      { to: "/meta", labelKey: "nav.favorites", icon: IconStar, favorites: true },
      { to: "/actors", labelKey: "nav.actors", icon: IconUsers },
      { to: "/catalog", labelKey: "nav.catalog", icon: IconCategory },
      { to: "/feeds", labelKey: "nav.feeds", icon: IconRss, end: true },
      { to: "/saved-queries", labelKey: "nav.savedQueries", icon: IconBookmark },
    ],
  },
  {
    key: "manage",
    labelKey: "nav.groups.manage",
    items: [
      { to: "/libraries", labelKey: "nav.libraries", icon: IconFolders },
      { to: "/plugins", labelKey: "nav.plugins", icon: IconPuzzle },
      { to: "/feeds/sources", labelKey: "nav.feedSources", icon: IconListTree },
    ],
  },
  {
    key: "ops",
    labelKey: "nav.groups.ops",
    items: [
      { to: "/tasks", labelKey: "nav.tasks", icon: IconListDetails },
      { to: "/schedules", labelKey: "nav.schedules", icon: IconClock },
      { to: "/logs", labelKey: "nav.logs", icon: IconFileText },
      { to: "/network", labelKey: "nav.networkCheck", icon: IconWorldSearch },
    ],
  },
];

function isNavActive(pathname: string, to: string, end = false): boolean {
  if (to === "/") return pathname === "/";
  if (end) return pathname === to || pathname === `${to}/`;
  return pathname === to || pathname.startsWith(`${to}/`);
}

/**
 * 收藏是片库的一个筛选视图 (与「只看某合集」同形), 因此与片库同路径, 只能从 search 区分.
 * 判据取自路由解析后的 search, 不解析 href: 数组参数由 router 序列化成 JSON (`user_tag_id=[1]`,
 * 多值 `[1,2]`, 序列化形态随版本变动), 而解析后的 `user_tag_id` 恒为 number[].
 */
function searchHasUserTag(search: object, favoriteTagId: number): boolean {
  if (!("user_tag_id" in search)) return false;
  const raw: unknown = search.user_tag_id;
  return Array.isArray(raw) && raw.some((value) => value === favoriteTagId);
}

function NavItemLink({
  item,
  onNavigate,
  favoriteTagId,
}: {
  item: NavItem;
  onNavigate?: () => void;
  favoriteTagId: number | null;
}) {
  const { t } = useTranslation("common");
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  const listDefaults = useUIStore((state) => state.listDefaults);
  const ensureFavoriteMutation = useMutation({ mutationFn: ensureFavoriteTagId });
  const Icon = item.icon;

  const favoritesActive = favoriteTagId != null && searchHasUserTag(search, favoriteTagId);
  // 两个入口共用一个路径, 高亮只能归一处: 带收藏参数时只亮收藏.
  const active = item.favorites
    ? favoritesActive
    : isNavActive(pathname, item.to, item.end) && !(item.to === "/meta" && favoritesActive);

  const shell = {
    label: t(item.labelKey),
    leftSection: <Icon size={18} stroke={1.6} />,
    active,
    variant: "filled" as const,
    style: { borderRadius: "var(--mantine-radius-md)" },
  };

  /** 落地为「片库 + 收藏标签」视图: 保留侧栏默认参数, 只叠加这一个筛选. */
  function openFavorites(tagId: number): void {
    void navigate({
      to: "/meta",
      search: { ...listDefaults.meta, user_tag_id: [tagId] },
    });
  }

  if (item.favorites) {
    return (
      <NavLink
        component="a"
        href={favoriteTagId != null ? `/meta?user_tag_id=${favoriteTagId}` : "/meta"}
        rightSection={ensureFavoriteMutation.isPending ? <Loader size={14} /> : undefined}
        onClick={(event) =>
          navItemClick(event, onNavigate, () => {
            if (favoriteTagId != null) {
              openFavorites(favoriteTagId);
              return;
            }
            // 还没收藏过任何影片: 标签不存在, 入口本身负责建标签.
            void ensureFavoriteMutation.mutateAsync().then(openFavorites);
          })
        }
        {...shell}
      />
    );
  }

  // 列表条目携带该页的默认参数: 入口地址不残留搜索词、页码与 agent 深链.
  // 路由的参数类型各不相同, 因此按 `to` 分支给字面量目标. 分类入口是种类索引页, 没有可承载的参数.
  // 片库与收藏两个入口用裸 `a` 而非 `Link`: 二者同路径, `Link` 会给它们都加 `aria-current="page"`,
  // 而 Mantine NavLink 把 `aria-current='page'` 与 `data-active` 并列当作激活态, 结果是两个一起高亮.
  if (item.to === "/meta") {
    return (
      <NavLink
        component="a"
        href="/meta"
        onClick={(event) =>
          navItemClick(
            event,
            onNavigate,
            () => void navigate({ to: "/meta", search: listDefaults.meta ?? {} }),
          )
        }
        {...shell}
      />
    );
  }
  if (item.to === "/actors") {
    return (
      <NavLink
        component={Link}
        to="/actors"
        onClick={(event) =>
          navItemClick(
            event,
            onNavigate,
            () => void navigate({ to: "/actors", search: listDefaults.actors ?? {} }),
          )
        }
        {...shell}
      />
    );
  }
  if (item.to === "/feeds") {
    return (
      <NavLink
        component={Link}
        to="/feeds"
        onClick={(event) =>
          navItemClick(
            event,
            onNavigate,
            () => void navigate({ to: "/feeds", search: listDefaults.feeds ?? {} }),
          )
        }
        {...shell}
        activeOptions={{ exact: true, includeSearch: false }}
      />
    );
  }
  return (
    <NavLink
      component={Link}
      to={item.to}
      onClick={onNavigate}
      {...shell}
      activeOptions={item.end ? { exact: true, includeSearch: false } : undefined}
    />
  );
}

function ThemeToggle() {
  const theme = useUIStore((s) => s.theme);
  const setTheme = useUIStore((s) => s.setTheme);
  const { t } = useTranslation("common");

  const next = theme === "light" ? "dark" : theme === "dark" ? "auto" : "light";
  const icon =
    theme === "light" ? (
      <IconSun size={18} />
    ) : theme === "dark" ? (
      <IconMoonStars size={18} />
    ) : (
      <IconSunMoon size={18} />
    );

  return (
    <HintedActionIcon
      variant="subtle"
      color="gray"
      size="lg"
      onClick={() => setTheme(next)}
      label={t(`theme.${theme}`)}
    >
      {icon}
    </HintedActionIcon>
  );
}

function LanguageMenu() {
  const { t, i18n } = useTranslation("common");
  const setLanguage = useUIStore((s) => s.setLanguage);

  return (
    <Menu position="bottom-end" shadow="md">
      <Menu.Target>
        <ActionIcon variant="subtle" color="gray" size="lg" aria-label="language">
          <IconLanguage size={18} />
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item
          onClick={() => {
            setLanguage("zh-CN");
            void i18n.changeLanguage("zh-CN");
          }}
        >
          {t("language.zh-CN")}
        </Menu.Item>
        <Menu.Item
          onClick={() => {
            setLanguage("en");
            void i18n.changeLanguage("en");
          }}
        >
          {t("language.en")}
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}

/**
 * 客户端设置入口: 服务器与登录态属于客户端, 不并入服务端配置. 只在壳内渲染 (判据见 lib/shell.ts).
 */
function ClientSettingsLink({ available }: { available: boolean }) {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  const { pathname } = useLocation();
  if (!available) return null;

  return (
    <HintedActionIcon
      variant="subtle"
      color={isNavActive(pathname, "/client") ? "brand" : "gray"}
      size="lg"
      onClick={() => void navigate({ to: "/client" })}
      label={t("nav.client")}
    >
      <IconDeviceMobile size={18} />
    </HintedActionIcon>
  );
}

function ConnectionIndicator() {
  const status = useConnectionStore((s) => s.status);
  const { t } = useTranslation("common");
  const color = status === "connected" ? "teal" : status === "reconnecting" ? "yellow" : "red";
  const labelKey =
    status === "connected"
      ? "status.connected"
      : status === "reconnecting"
        ? "status.reconnecting"
        : "status.disconnected";

  return (
    <HintedActionIcon variant="subtle" color="gray" size="lg" label={t(labelKey)}>
      <Box
        w={10}
        h={10}
        bg={`${color}.5`}
        style={{
          borderRadius: "50%",
          boxShadow:
            status === "reconnecting" ? `0 0 0 3px var(--mantine-color-${color}-2)` : undefined,
        }}
      />
    </HintedActionIcon>
  );
}

function HeaderSearch() {
  const { t } = useTranslation("metadata");
  const navigate = useNavigate();
  const location = useLocation();
  const [value, setValue] = useState("");

  // 快捷入口: 已在片库页时隐藏, 避免与页内搜索重复
  if (location.pathname === "/meta" || location.pathname.startsWith("/meta/")) {
    return <div style={{ flex: 1, minWidth: 0 }} />;
  }

  return (
    // 窄屏顶栏放不下搜索框, 入口回退到片库页内搜索.
    <Box
      component="form"
      visibleFrom="sm"
      style={{ flex: 1, minWidth: 0, maxWidth: 480, marginLeft: 24 }}
      onSubmit={(e) => {
        e.preventDefault();
        const q = value.trim();
        setValue("");
        void navigate({ to: "/meta", search: { q: q || undefined } });
      }}
    >
      <TextInput
        value={value}
        onChange={(e) => setValue(e.currentTarget.value)}
        placeholder={t("search.placeholder")}
        leftSection={<IconSearch size={16} />}
        radius="md"
      />
    </Box>
  );
}

function HeaderBrand() {
  return (
    <Group gap={8} wrap="nowrap" align="center">
      <Link to="/" style={{ textDecoration: "none", color: "inherit", whiteSpace: "nowrap" }}>
        <Group gap={8} wrap="nowrap" align="center">
          <img
            src="/favicon.svg"
            width={22}
            height={22}
            alt=""
            aria-hidden
            style={{ display: "block" }}
          />
          <Title order={4}>{APP_NAME}</Title>
        </Group>
      </Link>
      {/* 窄屏顶栏只保留品牌与图标动作, 版本号在侧栏内呈现; 不加包装元素, 避免改变宽屏下的行内基线. */}
      <VersionMenu visibleFrom="sm" />
    </Group>
  );
}

function GithubLink({ visibleFrom }: { visibleFrom?: "sm" }) {
  const { t } = useTranslation("common");

  return (
    <Tooltip label={t("about.github")}>
      <ActionIcon
        component="a"
        href={GITHUB_URL}
        target="_blank"
        rel="noreferrer"
        variant="subtle"
        color="gray"
        size="lg"
        visibleFrom={visibleFrom}
        aria-label={t("about.github")}
      >
        <IconBrandGithub size={18} />
      </ActionIcon>
    </Tooltip>
  );
}

export function AppShellLayout(): ReactNode {
  const { t } = useTranslation("common");
  const [mobileOpened, { toggle: toggleMobile, close: closeMobile }] = useDisclosure(false);
  const desktopCollapsed = useUIStore((s) => s.navbarCollapsed);
  const toggleDesktop = useUIStore((s) => s.toggleNavbar);
  const shell = shellEnvironment();
  const favoriteTagId = useFavoriteTagId().data ?? null;

  return (
    <AppShell
      header={{ height: APP_SHELL_HEADER_HEIGHT }}
      navbar={{
        width: 260,
        breakpoint: "sm",
        collapsed: { mobile: !mobileOpened, desktop: desktopCollapsed },
      }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" gap="sm" wrap="nowrap">
          <Burger opened={mobileOpened} onClick={toggleMobile} hiddenFrom="sm" size="sm" />
          <ActionIcon
            variant="subtle"
            color="gray"
            size="lg"
            visibleFrom="sm"
            onClick={toggleDesktop}
            aria-label="toggle navbar"
          >
            {desktopCollapsed ? (
              <IconLayoutSidebarLeftExpand size={18} />
            ) : (
              <IconLayoutSidebarLeftCollapse size={18} />
            )}
          </ActionIcon>
          <HeaderBrand />
          <HeaderSearch />
          <Group ml="auto" gap="xs" wrap="nowrap">
            <ConnectionIndicator />
            <ThemeToggle />
            <LanguageMenu />
            <ClientSettingsLink available={shell != null} />
            {/* 窄屏顶栏放不下外链, 该入口收进侧栏底部. */}
            <GithubLink visibleFrom="sm" />
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="sm">
        <ScrollArea
          style={{ flex: 1 }}
          offsetScrollbars
          // 侧栏滚到尽头时不许把滚动传给底下的页面 (触屏上尤其明显).
          viewportProps={{ style: { overscrollBehavior: "contain" } }}
        >
          {NAV_GROUPS.map((group) => (
            <div key={group.key} style={{ marginBottom: 16 }}>
              <Text
                size="xs"
                fw={700}
                c="dimmed"
                px="xs"
                mb={4}
                style={{ textTransform: "uppercase", letterSpacing: 0.5 }}
              >
                {t(group.labelKey)}
              </Text>
              {group.items.map((item) => (
                <NavItemLink
                  key={item.labelKey}
                  item={item}
                  onNavigate={closeMobile}
                  favoriteTagId={favoriteTagId}
                />
              ))}
            </div>
          ))}
        </ScrollArea>
        <Divider mb="sm" />
        <NavItemLink
          item={{ to: "/settings", labelKey: "nav.settings", icon: IconSettings }}
          onNavigate={closeMobile}
          favoriteTagId={favoriteTagId}
        />
        {/* 窄屏顶栏放不下版本与外链, 收进侧栏底部. */}
        <Group hiddenFrom="sm" gap="xs" px="xs" pt="sm" wrap="nowrap">
          <VersionMenu />
          <Box ml="auto">
            <GithubLink />
          </Box>
        </Group>
      </AppShell.Navbar>

      <AppShell.Main>
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
