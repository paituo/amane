import {
  Badge,
  Button,
  CopyButton,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconCheck,
  IconCircleCheck,
  IconCircleX,
  IconCopy,
  IconInfoCircle,
} from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  runConfigProbe,
  type ProbeEvent,
  type ProbeLevel,
  type ProbeOutcome,
} from "@/lib/config-probe";
import classes from "./probe-dialog.module.css";

interface ProbeDialogProps {
  section: string | null;
  /** 分区显示名, 由调用方翻译好传进来 (顶层分组名运行时才知). */
  label: string;
  /** 当前表单值; 可能含未保存的改动 —— 测试就是要验证"改完之后"的配置. */
  values: Record<string, unknown> | null;
  onClose: () => void;
}

type Status = "running" | "done" | "failed";

const LEVEL_ICON: Record<ProbeLevel, typeof IconInfoCircle> = {
  info: IconInfoCircle,
  ok: IconCircleCheck,
  warn: IconAlertTriangle,
  error: IconCircleX,
};

function formatLog(events: ProbeEvent[]): string {
  return events
    .map((event) => {
      const head = `${String(event.step_ms).padStart(6)}ms ${event.level.toUpperCase().padEnd(5)} ${event.message}`;
      return event.detail ? `${head}\n         ${event.detail}` : head;
    })
    .join("\n");
}

/**
 * 配置自检对话框: 打开即跑, 逐条显示服务端推来的步骤.
 *
 * 测的是"表单里现在这一份" (含未保存改动), 与线上配置是否已落盘无关.
 */
export function ProbeDialog({ section, label, values, onClose }: ProbeDialogProps) {
  const { t } = useTranslation("settings");
  const [attempt, setAttempt] = useState(0);

  return (
    <Modal
      opened={section !== null}
      onClose={onClose}
      title={t("probe.title", { section: label })}
      size="54rem"
      centered
      styles={{ body: { paddingTop: 4 } }}
    >
      {section !== null && (
        // 每次重测换 key 重挂载: 事件列表、计时与状态都随之归零, 不必在 effect 里 setState 清理.
        <ProbeRun
          key={`${section}#${attempt}`}
          section={section}
          values={values}
          onRerun={() => setAttempt((value) => value + 1)}
        />
      )}
    </Modal>
  );
}

function ProbeRun({
  section,
  values,
  onRerun,
}: {
  section: string;
  values: Record<string, unknown> | null;
  onRerun: () => void;
}) {
  const { t } = useTranslation("settings");
  const [events, setEvents] = useState<ProbeEvent[]>([]);
  const [outcome, setOutcome] = useState<ProbeOutcome | null>(null);
  const [status, setStatus] = useState<Status>("running");
  const [error, setError] = useState<string | null>(null);
  const [startedAt] = useState(() => Date.now());
  const [now, setNow] = useState(startedAt);
  const viewport = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    runConfigProbe({
      section,
      values,
      signal: controller.signal,
      onEvent: (event) => setEvents((prev) => [...prev, event]),
    })
      .then((result) => {
        setOutcome(result);
        setStatus(result.ok ? "done" : "failed");
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : String(cause));
        setStatus("failed");
      });
    return () => controller.abort();
  }, [section, values]);

  // 计时只在跑的时候走: 结束后停在服务端给的总耗时上, 便于对着日志核对.
  useEffect(() => {
    if (status !== "running") return;
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, [status]);

  useEffect(() => {
    const node = viewport.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [events.length]);

  const elapsed = status === "running" ? Math.max(now - startedAt, 0) : (outcome?.elapsed_ms ?? 0);

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="center" wrap="wrap">
        <Group gap="sm">
          {status === "running" ? (
            <Group gap={6}>
              <Loader size="xs" />
              <Text size="sm" c="dimmed">
                {t("probe.running")}
              </Text>
            </Group>
          ) : status === "done" ? (
            <Badge color="teal" variant="light" leftSection={<IconCircleCheck size={12} />}>
              {t("probe.passed")}
            </Badge>
          ) : (
            <Badge color="red" variant="light" leftSection={<IconCircleX size={12} />}>
              {t("probe.failed")}
            </Badge>
          )}
          <Text size="xs" c="dimmed">
            {t("probe.elapsed", { ms: elapsed })}
          </Text>
        </Group>
        <Group gap="xs">
          <Button
            size="compact-sm"
            variant="default"
            disabled={status === "running"}
            onClick={onRerun}
          >
            {t("probe.rerun")}
          </Button>
          <CopyButton value={formatLog(events)} timeout={1500}>
            {({ copied, copy }) => (
              <Tooltip label={copied ? t("probe.copied") : t("probe.copy")} withArrow>
                <Button
                  size="compact-sm"
                  variant="default"
                  onClick={copy}
                  leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                >
                  {copied ? t("probe.copied") : t("probe.copy")}
                </Button>
              </Tooltip>
            )}
          </CopyButton>
        </Group>
      </Group>

      <Text size="xs" c="dimmed">
        {t("probe.scopeHint")}
      </Text>

      <ScrollArea.Autosize mah={380} viewportRef={viewport} className={classes.log} type="auto">
        {events.length === 0 && status === "running" ? (
          <Text size="sm" c="dimmed" className={classes.empty}>
            {t("probe.waiting")}
          </Text>
        ) : (
          <Stack gap={0}>
            {events.map((event) => {
              const Icon = LEVEL_ICON[event.level] ?? IconInfoCircle;
              return (
                <div key={event.seq} className={classes.row}>
                  <span className={classes.elapsed}>+{event.step_ms}ms</span>
                  <span className={classes.icon} data-level={event.level}>
                    <Icon size={15} />
                  </span>
                  <div className={classes.body}>
                    <div className={classes.message}>{event.message}</div>
                    {event.detail && <pre className={classes.detail}>{event.detail}</pre>}
                  </div>
                </div>
              );
            })}
          </Stack>
        )}
      </ScrollArea.Autosize>

      {error && (
        <Text size="sm" c="red">
          {t("probe.requestFailed", { message: error })}
        </Text>
      )}
    </Stack>
  );
}
