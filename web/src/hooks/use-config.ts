import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  getConfigOptions,
  getConfigQueryKey,
  getLocalPlayerQueryKey,
  updateConfigMutation,
} from "@/client/@tanstack/react-query.gen";
import { getConfigSchema } from "@/client/sdk.gen";
import type { JSONSchemaObject } from "@/components/schema-form/schema";
import { useAppMutation } from "@/hooks/use-app-mutation";

export function useConfigSchema() {
  return useQuery({
    queryKey: ["config-schema"],
    queryFn: async () => {
      const { data } = await getConfigSchema();
      // OpenAPI schema 运行时对象 → Schema 表单内部类型.
      return data as JSONSchemaObject;
    },
  });
}

export function useConfig() {
  return useQuery(getConfigOptions());
}

export function useUpdateConfig() {
  const { t } = useTranslation("common");
  return useAppMutation({
    mutationOptions: updateConfigMutation(),
    // 播放器开关在设置页改, 但本机播放状态走独立的 `/api/playback/local-player` 端点缓存
    // (staleTime 60s). 不在这里失效, 关掉外部播放器后最长还要等 60s, 期间封面按钮仍按旧的
    // `enabled=true` 去拉起本机播放, 后端已禁用就会报 "外部播放未启用". 一并失效, 设置保存后立刻对齐.
    invalidates: [getConfigQueryKey(), getLocalPlayerQueryKey()],
    successToast: t("toast.configSaved"),
  });
}
