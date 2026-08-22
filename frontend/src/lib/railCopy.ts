/**
 * Static copy the rail renders outside its four destinations: the brand
 * wordmark, the project-switcher slot, the Settings/Profile footer, and the
 * shared "(later)" marker every deferred rail item wears. Owned here, not
 * inline in WorkshopShell, so REQ-UIQ-04's pseudo-locale check can see that
 * every rail string has one owner instead of a second hardcoded copy.
 */
export const railCopy = {
  brand: "atelier",
  later: "(later)",
  switchProject: "switch project",
  switchProjectHint: "One project today — a real switcher is a later #133 slot.",
  settings: "Settings",
  settingsHint: "Professional settings surface — not built yet. REQ-UI-15.",
  profile: "Profile",
  profileHint: "Profile needs login/OIDC — not built yet. #82."
} as const;
