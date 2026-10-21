import { createFileRoute } from "@tanstack/react-router";
import { z } from "zod";
import { MetadataDetail } from "@/components/media/metadata-detail";

/**
 * `t` 是评论时间戳跳转的目标秒数: 写进地址栏以便分享与刷新后定位, 因此可以非整数以外的任何值
 * 都按未提供处理.
 */
const metaDetailSearchSchema = z.object({
  t: z.coerce.number().int().min(0).optional().catch(undefined),
});

export const Route = createFileRoute("/meta/$metadataId")({
  validateSearch: metaDetailSearchSchema,
  component: TitleDetailPage,
});

/** 详情路由只负责取参数与写地址栏: 主体在同名的共享组件里, 与就地打开的详情弹窗同源. */
function TitleDetailPage() {
  const { metadataId } = Route.useParams();
  const { t: seekSeconds } = Route.useSearch();
  const navigate = Route.useNavigate();

  return (
    <MetadataDetail
      metadataId={metadataId}
      seekSeconds={seekSeconds}
      onSeekCommit={(seconds) =>
        void navigate({
          // replace: 时间戳是定位而不是导航, 不该在历史里堆一串记录.
          // resetScroll: 路由默认在位置提交后把页面滚动到顶部, 与定位的语义冲突; 滚动由播放器按需调整.
          search: (prev) => ({ ...prev, t: seconds }),
          replace: true,
          resetScroll: false,
        })
      }
    />
  );
}
