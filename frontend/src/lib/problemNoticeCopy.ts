/**
 * Copy the two-layer problem notice speaks: the visible title and message,
 * and the technical reveal behind them. Shared by every room that hosts
 * `ProblemNotice`.
 */
export const problemNoticeCopy = {
  title: "Request failed",
  message: "The request could not be completed.",
  technicalDetail: "Technical detail",
  http: "HTTP"
} as const;
