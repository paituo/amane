import { Box, Radio, Stack } from "@mantine/core";
import type { AnyFieldApi } from "@tanstack/react-form";
import { useEnumHintI18n, useEnumI18n } from "../hooks";
import type { FieldProps, TextEnumJSONSchema } from "../schema";
import { FieldChrome } from "./field-chrome";

/**
 * 一行一个选项的单选列表: 标题与说明在左, 单选框在右.
 *
 * 与 `EnumToggle` 的差别只在版式: 选项各自带一句说明时, 横排的段控件放不下, 说明只能压成一行小字.
 * 逐项说明取 `<fieldPath>.hints.<option>`, 缺失时只显示标题.
 *
 * 由 schema 的 `x-widget: "RadioRows"` 选中, 因此后端只需要在字段上加这个标记.
 */
export function RadioRowsField({
  name,
  label,
  description,
  schema,
  form,
  i18nPath,
  i18nPrefix,
  variant,
}: FieldProps<TextEnumJSONSchema>) {
  const getOptionLabel = useEnumI18n(i18nPath, i18nPrefix);
  const getOptionHint = useEnumHintI18n(i18nPath, i18nPrefix);
  const options = schema.enum;

  return (
    <form.Field name={name}>
      {(field: AnyFieldApi) => (
        <FieldChrome variant={variant} label={label} description={description}>
          <Radio.Group
            value={typeof field.state.value === "string" ? field.state.value : null}
            onChange={(value) => field.handleChange(value)}
          >
            <Stack gap={0}>
              {options.map((option, index) => (
                <Box
                  key={option}
                  py="sm"
                  style={{
                    borderTop:
                      index === 0 ? undefined : "1px solid var(--mantine-color-default-border)",
                  }}
                >
                  <Radio
                    value={option}
                    // 标题与说明交给 Radio 自带的 label/description, 点标题即可选中, 不必再套一层点击区.
                    label={getOptionLabel(option, index, schema)}
                    description={getOptionHint(option)}
                    labelPosition="left"
                    styles={{ labelWrapper: { flex: 1 } }}
                  />
                </Box>
              ))}
            </Stack>
          </Radio.Group>
        </FieldChrome>
      )}
    </form.Field>
  );
}
