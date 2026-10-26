import { Checkbox, Divider, Group, Menu, Stack, Text } from "@mantine/core";
import { IconCheck, IconChevronDown } from "@tabler/icons-react";
import type { SearchField, SearchType } from "@/client/types.gen";
import { HintedActionIcon } from "@/components/common/hinted-action-icon";

export interface SearchOptionsMenuProps {
  /** 当前检索方式. */
  searchType: SearchType;
  /** 检索方式选项; 顺序即菜单顺序. */
  modes: readonly { value: SearchType; label: string }[];
  onSearchTypeChange: (next: SearchType) => void;
  /** 已选字段. 空集不被允许, 由本组件拦住最后一次取消勾选. */
  fields: readonly SearchField[];
  /** 全部可选字段; 顺序即复选框顺序. */
  fieldOptions: readonly { value: SearchField; label: string }[];
  onFieldsChange: (next: SearchField[]) => void;
  /** 请求的路没走成 (例如向量未启用): 菜单里给一句说明, 免得结果被误读成语义检索. */
  degraded?: boolean;
  label: string;
  modeLabel: string;
  fieldsLabel: string;
  degradedHint: string;
  lastFieldHint: string;
}

/**
 * 检索框里的向下箭头: 检索方式单选 + 搜索字段复选.
 *
 * 字段只作用于关键词路 —— 向量是整条影片的嵌入, 查询时切不出字段范围. 这一点不靠隐藏选项表达:
 * 勾了却悄悄不生效比勾不了更费解.
 */
export function SearchOptionsMenu({
  searchType,
  modes,
  onSearchTypeChange,
  fields,
  fieldOptions,
  onFieldsChange,
  degraded = false,
  label,
  modeLabel,
  fieldsLabel,
  degradedHint,
  lastFieldHint,
}: SearchOptionsMenuProps) {
  const onlyOne = fields.length === 1;
  return (
    <Menu shadow="md" width={240} position="bottom-end" withinPortal>
      <Menu.Target>
        {/* 竖分隔线把箭头划成一个独立控件: 只靠一个淡色 chevron 时, 它看着像输入框的装饰而不是按钮. */}
        <Group gap={2} wrap="nowrap" pr={2}>
          <Divider orientation="vertical" h={16} />
          <HintedActionIcon variant="subtle" color="gray.7" label={label}>
            <IconChevronDown size={16} />
          </HintedActionIcon>
        </Group>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>{modeLabel}</Menu.Label>
        {modes.map((mode) => (
          <Menu.Item
            key={mode.value}
            fw={mode.value === searchType ? 600 : undefined}
            rightSection={mode.value === searchType ? <IconCheck size={14} /> : undefined}
            onClick={() => onSearchTypeChange(mode.value)}
          >
            {mode.label}
          </Menu.Item>
        ))}
        <Divider />
        <Menu.Label>{fieldsLabel}</Menu.Label>
        <Checkbox.Group
          value={[...fields]}
          onChange={(next) => onFieldsChange(next as SearchField[])}
          px="xs"
          pb={onlyOne ? 4 : "xs"}
        >
          <Stack gap={4}>
            {fieldOptions.map((option) => {
              const isLast = onlyOne && fields.includes(option.value);
              return (
                <Checkbox
                  key={option.value}
                  value={option.value}
                  size="sm"
                  label={option.label}
                  // 一个都不选在关键词路上恒等于 0 命中, 在向量路上又不起作用; 与其让这次
                  // 勾选看起来生效, 不如直接不让它发生.
                  disabled={isLast}
                />
              );
            })}
          </Stack>
        </Checkbox.Group>
        {onlyOne && (
          <Text size="xs" c="dimmed" px="xs" pb="xs">
            {lastFieldHint}
          </Text>
        )}
        {degraded && (
          <>
            <Divider />
            <Text size="xs" c="dimmed" px="xs" py={4}>
              {degradedHint}
            </Text>
          </>
        )}
      </Menu.Dropdown>
    </Menu>
  );
}
