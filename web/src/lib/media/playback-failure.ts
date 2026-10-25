/**
 * 播放失败的分类与判定.
 *
 * 独立于播放器组件: 判定在点播面板里执行, 而播放器与 media-chrome、hls.js 一并动态导入 —— 面板若从播放器
 * 模块取运行时值, 这些依赖会被一起打进主包.
 */

/** hls.js 致命错误中构成提示文案的字段, 后端没有给出 detail 时使用. */
export type HlsFailure = {
  details: string;
  status: number | null;
  target: string;
};

/**
 * 播放失败的原因.
 *
 * `hls` 是 hls.js 报告的致命错误 (含失败分片与响应状态), `media` 是媒体元素自身的错误 (`<video>` 的
 * `error`, 只在原生播放路径上出现), `engine` 表示浏览器缺少可用的播放引擎 — hls.js 判定自身不可用.
 *
 * 播放器只报告事实, 不判断应当如何提示; 分类见 `isCodecFailure`.
 */
export type PlaybackFailure =
  | { kind: "hls"; hls: HlsFailure }
  | {
      kind: "media";
      /** `MediaError.code`; 元素没有给出错误对象时为空. */
      code: number | null;
      /** 浏览器给出的原因 (`MediaError.message`), 例如 `DEMUXER_ERROR_NO_SUPPORTED_STREAMS`; 通常为空串. */
      detail: string | null;
    }
  | { kind: "engine" };

/** `MediaError.code` 的两个取值: 解码失败与源不受支持. 取值见 HTML 规范的错误码表. */
const MEDIA_ERR_DECODE = 3;
const MEDIA_ERR_SRC_NOT_SUPPORTED = 4;

/**
 * hls.js 中表示编码不受支持的错误详情: `bufferAddCodecError` 是 MSE 拒绝该编码 (SourceBuffer 无法处理
 * 这种编解码器), `manifestIncompatibleCodecsError` 是清单声明了当前环境没有对应解码器的编码; 两者都带
 * `Codec`.
 */
const HLS_CODEC_DETAILS = /codec/i;

/**
 * 这次失败是否属于"浏览器解不了这条流的编码", 而不是网络、鉴权或服务端故障.
 *
 * 判据:
 * - `engine`: 浏览器没有 MSE, 两条播放路径都不可用.
 * - `hls`: 错误详情表明编码不受支持.
 * - `media`: `MEDIA_ERR_DECODE` 或 `MEDIA_ERR_SRC_NOT_SUPPORTED`. 后者的成因不止编码 (404、非媒体响应
 *   同样是它), 因此调用方必须先确认服务端能给出字节再采用本判定, 见点播面板里的 `probePlayback`.
 *
 * 判定的用途是选择提示与兜底入口: 编码不受支持时重试不会改变结果, 应引导外部播放器或换一条码流.
 */
export function isCodecFailure(failure: PlaybackFailure | null): boolean {
  if (failure == null) {
    return false;
  }
  if (failure.kind === "engine") {
    return true;
  }
  if (failure.kind === "hls") {
    return HLS_CODEC_DETAILS.test(failure.hls.details);
  }
  return failure.code === MEDIA_ERR_DECODE || failure.code === MEDIA_ERR_SRC_NOT_SUPPORTED;
}
