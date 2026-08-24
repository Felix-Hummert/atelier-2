/**
 * Static copy the rail renders outside its four destinations: the brand
 * wordmark, the project-switcher slot, the Settings/Profile footer, and the
 * shared "Not built yet" marker every deferred rail item wears -- plain
 * words, not a parenthesised fragment the operator has to decode (operator
 * ruling 23.08.). Owned here, not inline in WorkshopShell, so REQ-UIQ-04's
 * pseudo-locale check can see that every rail string has one owner instead of
 * a second hardcoded copy.
 */
export const railCopy = {
  brand: "atelier",
  later: "Not built yet",
  switchProject: "switch project",
  switchProjectHint: "One project today — a real switcher comes later.",
  settings: "Settings",
  settingsHint: "Professional settings surface — not built yet. REQ-UI-15.",
  profile: "Profile",
  profileHint: "Profile needs login/OIDC — not built yet.",
  runningBadgeSuffix: "running",
  needsYouBadgeSuffix: "needs you"
} as const;
