import { ActionIcon, Autocomplete, Group, Text } from "@mantine/core";
import { IconSearch, IconX } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useUIStore } from "@/stores/ui";

/** 下拉里"清除全部历史"这一项的取值. 用不可打印字符开头, 不会与真实检索词冲突. */
const CLEAR_ALL = "\u0000clear-history";

/** 哨兵前缀; 凡写回输入框的值含它一律视为"这不是用户输入". */
const SENTINEL = "\u0000";

const NO_TERMS: string[] = [];

/**
 * 带最近搜索词的检索框.
 *
 * 输入即检索 (由调用方防抖), 下拉列出这个列表入口最近用过的词; 每项右侧的叉只删那一项, 末项清空整份
 * 历史. 历史存在 `stores/ui.ts`, 与列表态无关: 关掉"还原列表"不影响它.
 */
export function SearchInput({
  value,
  onChange,
  onSubmit,
  placeholder,
  historyKey,
  rightSection,
}: {
  value: string;
  onChange: (value: string) => void;
  /** 选中历史词时立即检索, 不必再敲回车. */
  onSubmit?: (value: string) => void;
  placeholder?: string;
  /** 列表入口标识; 不同的入口各记各的历史. */
  historyKey: string;
  /** 输入框右侧的附加控件 (如检索方式菜单). 传了才放开指针事件. */
  rightSection?: ReactNode;
}) {
  const { t } = useTranslation("metadata");
  const terms = useUIStore((state) => state.recentSearches[historyKey] ?? NO_TERMS);
  const rememberSearch = useUIStore((state) => state.rememberSearch);
  const forgetSearch = useUIStore((state) => state.forgetSearch);
  const clearSearches = useUIStore((state) => state.clearSearches);

  // "清除全部历史"这一项的 label 故意与 value 同值 (含不可打印前缀): Mantine 点完选项后会无条件把
  // `optionsLockup[val].label` 写回输入框, 拿哨兵值去接就必然被 `handleValueChange` 吞掉, 输入框与检索
  // 词都不会被污染. 真正显示什么由 renderOption 决定.
  const data = [
    ...terms.map((term) => ({ value: term, label: term })),
    ...(terms.length > 0 ? [{ value: CLEAR_ALL, label: CLEAR_ALL }] : []),
  ];

  /** Mantine 会把选中项的 label 写回输入框; 哨兵值一律丢弃. */
  function handleChange(next: string) {
    if (next.includes(SENTINEL)) return;
    onChange(next);
  }

  function submit(term: string) {
    if (term === CLEAR_ALL) {
      clearSearches(historyKey);
      return;
    }
    rememberSearch(historyKey, term);
    onChange(term);
    onSubmit?.(term);
  }

  return (
    <Autocomplete
      value={value}
      onChange={handleChange}
      data={data}
      placeholder={placeholder}
      leftSection={<IconSearch size={16} />}
      rightSection={rightSection}
      rightSectionPointerEvents={rightSection ? "all" : undefined}
      w="100%"
      // 默认过滤按 label 猜, 会把"清除全部历史"整项滤掉 (它的显示文案不在 label 里), 输入框一旦有字就再也
      // 点不到"全部清除". 这里自己滤: 历史词按输入收窄, 清除项常驻.
      filter={({ options, search }) => {
        const needle = search.trim().toLowerCase();
        if (needle === "") return options;
        return options.filter(
          (option) =>
            // 分组项没有 value, 直接放过 (本组件不产生分组).
            !("value" in option) ||
            option.value === CLEAR_ALL ||
            option.value.toLowerCase().includes(needle),
        );
      }}
      // 回车直接检索, 顺手把当前词记进历史; 与选中历史项同一路径.
      onKeyDown={(event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        submit(value);
      }}
      onOptionSubmit={submit}
      renderOption={({ option }) =>
        option.value === CLEAR_ALL ? (
          <Text size="sm" c="dimmed">
            {t("search.clearHistory")}
          </Text>
        ) : (
          <Group justify="space-between" w="100%" wrap="nowrap" gap="xs">
            <Text size="sm" truncate="end">
              {option.value}
            </Text>
            <ActionIcon
              size="xs"
              variant="subtle"
              color="gray"
              aria-label={t("search.forgetTerm")}
              // 按下就拦掉, 否则这一下会先被当成"选中该项".
              onMouseDown={(event) => {
                event.preventDefault();
                event.stopPropagation();
                forgetSearch(historyKey, option.value);
              }}
            >
              <IconX size={12} />
            </ActionIcon>
          </Group>
        )
      }
    />
  );
}
