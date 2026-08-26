/**
 * Copy the Settings room renders.
 *
 * Settings is the context above the three rooms, not a fourth room (ADR 0019
 * §1): its head is the project itself, and beneath it stands what belongs to
 * the project rather than to a room. It carries no door into a room — the rail
 * is that door — so nothing here repeats what the Workbench, the Catalog or
 * History already own.
 *
 * The connected sources, the model registry and the model defaults the picture
 * draws in this room are not built yet; what stands here today is how much work
 * the project holds and which agent it reaches for by default.
 */
export const settingsPageCopy = {
  workTitle: "Work in this project",
  noRuns: "No runs in this project yet.",
  noRunsNext: "Start one from the Catalog.",
  occupancyEyebrow: "Project defaults",
  occupancyTitle: "Who does the work",
  occupancyDescription:
    "The agent this project reaches for by default when a workflow asks for a role.",
  occupancyUnavailable: "Project defaults unavailable",
  runsUnavailable: "Project runs unavailable",
  runsIncomplete: "Project runs incomplete"
} as const;
