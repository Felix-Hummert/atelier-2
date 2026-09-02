<script lang="ts">
  import { onMount } from "svelte";
  import { SvelteSet } from "svelte/reactivity";

  import {
    type AgentConfigurationInput,
    type AgentConfigurationRevision,
    type AgentConfigurationRevisionListItem,
    type AuthProfileRevision,
    type CockpitApi,
    type ExactModelRegistryRevisionWrite,
    type ExactProjectModelDefaultsRevisionWrite,
    type ModelRegistryRevision,
    type ProjectModelDefaultsRevision,
    type ProjectSourceResource
  } from "../api/client";
  import AddModelSheet from "../components/AddModelSheet.svelte";
  import ConnectSourceSheet from "../components/ConnectSourceSheet.svelte";
  import ProviderAccounts from "../components/ProviderAccounts.svelte";
  import DisconnectSourceSheet from "../components/DisconnectSourceSheet.svelte";
  import ReadState from "../components/ReadState.svelte";
  import RenewSourceTokenSheet from "../components/RenewSourceTokenSheet.svelte";
  import {
    defaultsAfterRemovingConfigurationHash,
    offeredAccounts,
    planAddModel,
    rebasedRegistryWrite,
    rowPresentation,
    trimmedModelId,
    type RegistryIntent
  } from "../lib/addModel";
  import { presentProviderAccounts } from "../lib/providerAccounts";
  import {
    connectProjectSourceBody,
    disconnectFacts,
    presentProjectSource,
    rotateProjectSourceTokenBody,
    sourceWriteFailure,
    takeActiveSourcesToday,
    type DisconnectFacts,
    type SourceDoorError,
    type SourceWriteDoor
  } from "../lib/projectSources";
  import { problemCode } from "../lib/catalogName";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { THE_ONE_PROJECT } from "../lib/project";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { readEveryAgentConfiguration, readEveryAuthProfile } from "../lib/runPages";
  import {
    accountChoice,
    difficultyLabel,
    noSuchModel,
    retainedAccountChoice,
    settingsPageCopy,
    sourceAlreadyPresent
  } from "../lib/settingsPageCopy";

  export let cockpitApi: CockpitApi;

  const difficulties = [3, 2, 1] as const;
  type Difficulty = typeof difficulties[number];
  type SettingsFailure = { kind: "unavailable"; title: string };

  interface SettingsSnapshot {
    projectReference: string;
    sources: ProjectSourceResource[];
    severalNotBuilt: boolean;
    configurations: AgentConfigurationRevisionListItem[];
    profiles: AuthProfileRevision[];
    registries: ModelRegistryRevision[];
    defaults: ProjectModelDefaultsRevision | null;
  }

  type PublishedAdd = {
    revision: AgentConfigurationRevision;
    pin: AgentConfigurationRevisionListItem;
  };

  type FailedWrite =
    | {
        kind: "registry";
        providerId: string;
        write: ExactModelRegistryRevisionWrite;
        intent: RegistryIntent;
        rebase: boolean;
      }
    | {
        kind: "remove";
        projectReference: string;
        defaultsWrite: ExactProjectModelDefaultsRevisionWrite;
        providerId: string;
        registryWrite: ExactModelRegistryRevisionWrite;
        intent: RegistryIntent;
      }
    | {
        kind: "check-publish";
        providerId: string;
        write: ExactModelRegistryRevisionWrite;
        configurationHash: string;
        intent: RegistryIntent;
        rebase: boolean;
        published: PublishedAdd | null;
      }
    | {
        kind: "validation";
        providerId: string;
        configurationHash: string;
      }
    | {
        kind: "configuration";
        input: AgentConfigurationInput;
      }
    | {
        kind: "defaults";
        projectReference: string;
        write: ExactProjectModelDefaultsRevisionWrite;
      };

  type RegistryPublish =
    | { kind: "ok" }
    | { kind: "uncertain"; write: ExactModelRegistryRevisionWrite }
    | { kind: "conflict" };

  type SheetPrefill = { authProfileRevisionHash: string; modelId: string };

  let settings: RetainedRead<SettingsSnapshot, SettingsFailure> = retainedRead();
  let selections: Partial<Record<Difficulty, string>> = {};
  let writing = false;
  let failedWrite: FailedWrite | null = null;
  let sheetOpen = false;
  let sheetPrefill: SheetPrefill | null = null;
  let checkingHashes = new SvelteSet<string>();
  let duplicateNotice: { providerId: string; modelId: string } | null = null;
  let sourceSheet:
    | { kind: "connect" }
    | { kind: "disconnect"; source: ProjectSourceResource; facts: DisconnectFacts }
    | { kind: "renew"; source: ProjectSourceResource }
    | null = null;
  let sourceError: SourceDoorError | null = null;
  let sourceBusy = false;
  let duplicateSourceReference: string | null = null;
  let lastConnectAddress: string | null = null;

  $: mutationsFrozen = writing || failedWrite !== null;
  $: accountOptions = settings.confirmed === null
    ? []
    : offeredAccounts(settings.confirmed.profiles, settings.confirmed.configurations);
  $: sourceRows = (settings.confirmed?.sources ?? []).map((source) =>
    presentProjectSource(
      source,
      new Date(),
      source.public_source_reference === duplicateSourceReference
    )
  );
  $: accountRows = presentProviderAccounts(settings.confirmed?.profiles ?? []);

  onMount(() => { void load(); });

  async function load(): Promise<void> {
    const begun = beginRead(settings);
    settings = begun.read;
    failedWrite = null;
    sourceSheet = null;
    sourceError = null;
    sourceBusy = false;
    duplicateSourceReference = null;
    lastConnectAddress = null;
    try {
      const projects = await cockpitApi.listProjects();
      const projectReference = projects.items[0]?.public_project_reference;
      if (projectReference === undefined) throw new Error("served project missing");

      const [sourceList, configurationReading, profileReading] = await Promise.all([
        cockpitApi.listProjectSources(projectReference),
        readEveryAgentConfiguration((after) => cockpitApi.listAgentConfigurationRevisions(after)),
        readEveryAuthProfile((after) => cockpitApi.listAuthProfileRevisions(after))
      ]);
      if (!configurationReading.complete) throw new Error("model listing incomplete");
      if (!profileReading.complete) throw new Error("account listing incomplete");
      const profiles = profileReading.profiles;

      const providers = [...new Set(
        configurationReading.configurations.map((configuration) => configuration.provider_id)
      )].sort();
      const registries = (await Promise.all(
        providers.map((providerId) => cockpitApi.getModelRegistry(providerId).catch((error: unknown) => {
          if (problemCode(error) === "model-registry-missing") return null;
          throw error;
        }))
      )).filter((registry): registry is ModelRegistryRevision => registry !== null);
      const defaults = await cockpitApi.getProjectModelDefaults(projectReference).catch(
        (error: unknown) => {
          if (problemCode(error) === "project-model-defaults-missing") return null;
          throw error;
        }
      );
      const taken = takeActiveSourcesToday(sourceList.items);
      selections = Object.fromEntries(
        (defaults?.defaults ?? []).map((item) => [
          item.difficulty,
          item.agent_configuration_revision_hash
        ])
      );
      settings = confirmRead(settings, begun.generation, {
        projectReference,
        sources: taken.items,
        severalNotBuilt: taken.severalNotBuilt,
        configurations: configurationReading.configurations,
        profiles,
        registries,
        defaults
      });
    } catch {
      settings = failRead(settings, begun.generation, {
        kind: "unavailable",
        title: wrapDisplayCopy(settingsPageCopy.unavailable)
      });
    }
  }

  function registryFor(providerId: string): ModelRegistryRevision | null {
    return settings.confirmed?.registries.find(
      (registry) => registry.provider_id === providerId
    ) ?? null;
  }

  function entryFor(configurationHash: string) {
    for (const registry of settings.confirmed?.registries ?? []) {
      const entry = registry.entries.find(
        (candidate) => candidate.agent_configuration_revision_hash === configurationHash
      );
      if (entry !== undefined) return { registry, entry };
    }
    return null;
  }

  function accountFor(configuration: AgentConfigurationRevisionListItem): string {
    return settings.confirmed?.profiles.find(
      (profile) => profile.auth_profile_revision_hash === configuration.auth_profile_revision_hash
    )?.profile_id ?? settingsPageCopy.unknownAccount;
  }

  function updateRegistry(result: ModelRegistryRevision): void {
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    settings = {
      ...settings,
      confirmed: {
        ...snapshot,
        registries: [
          ...snapshot.registries.filter((registry) => registry.provider_id !== result.provider_id),
          result
        ].sort((left, right) => left.provider_id.localeCompare(right.provider_id))
      }
    };
  }

  function appendConfiguration(
    revision: AgentConfigurationRevision,
    pin: AgentConfigurationRevisionListItem
  ): void {
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    if (snapshot.configurations.some(
      (item) => item.agent_configuration_revision_hash === revision.agent_configuration_revision_hash
    )) return;
    settings = {
      ...settings,
      confirmed: {
        ...snapshot,
        configurations: [
          ...snapshot.configurations,
          {
            ...revision,
            startable: pin.startable,
            structurally_startable: pin.structurally_startable,
            not_startable_reason: pin.not_startable_reason
          }
        ]
      }
    };
  }

  function closeSheet(): void {
    sheetOpen = false;
  }

  function openAddSheet(prefill: SheetPrefill | null): void {
    sheetPrefill = prefill;
    sheetOpen = true;
  }

  function replaceSources(
    sources: ProjectSourceResource[],
    severalNotBuilt?: boolean
  ): void {
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    settings = {
      ...settings,
      confirmed: {
        ...snapshot,
        sources,
        severalNotBuilt: severalNotBuilt ?? snapshot.severalNotBuilt
      }
    };
  }

  function nameOneSourceToday(): void {
    sourceError = {
      sentence: settingsPageCopy.oneSourceToday,
      nextStep: null
    };
  }

  function sourceFor(publicSourceReference: string): ProjectSourceResource | null {
    return settings.confirmed?.sources.find(
      (item) => item.public_source_reference === publicSourceReference
    ) ?? null;
  }

  async function recoverDuplicateSource(attemptedAddress: string): Promise<void> {
    const snapshot = settings.confirmed;
    const existing = snapshot?.sources.find((item) => item.address === attemptedAddress);
    if (existing !== undefined) {
      duplicateSourceReference = existing.public_source_reference;
      sourceSheet = null;
      sourceError = null;
      return;
    }
    if (snapshot !== null && snapshot.sources.length > 0) {
      nameOneSourceToday();
      return;
    }
    if (snapshot !== null) {
      try {
        const listed = await cockpitApi.listProjectSources(snapshot.projectReference);
        const found = listed.items.find((item) => item.address === attemptedAddress);
        if (found !== undefined) {
          replaceSources([found], listed.items.length > 1);
          duplicateSourceReference = found.public_source_reference;
          sourceSheet = null;
          sourceError = null;
          return;
        }
        const taken = takeActiveSourcesToday(listed.items);
        replaceSources(taken.items, taken.severalNotBuilt);
        if (taken.items.length > 0) {
          nameOneSourceToday();
          return;
        }
      } catch {
        /* list failed; fall through to the owned Retry brick */
      }
    }
    sourceError = {
      sentence: settingsPageCopy.sourceNotShown,
      nextStep: settingsPageCopy.retry
    };
  }

  async function rememberSourceError(
    error: unknown,
    door: SourceWriteDoor,
    attemptedAddress?: string
  ): Promise<void> {
    const failure = sourceWriteFailure(error, door);
    if (failure.kind === "duplicate") {
      await recoverDuplicateSource(attemptedAddress ?? lastConnectAddress ?? "");
      return;
    }
    sourceError = { sentence: failure.sentence, nextStep: failure.nextStep };
  }

  async function retryDuplicateSource(): Promise<void> {
    if (sourceBusy || lastConnectAddress === null) return;
    sourceBusy = true;
    try {
      await recoverDuplicateSource(lastConnectAddress);
    } finally {
      sourceBusy = false;
    }
  }

  function openConnectSheet(): void {
    if (sourceBusy) return;
    sourceError = null;
    sourceSheet = { kind: "connect" };
  }

  function openDisconnectSheet(source: ProjectSourceResource): void {
    if (sourceBusy) return;
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    sourceError = null;
    sourceSheet = {
      kind: "disconnect",
      source,
      facts: disconnectFacts({
        address: source.address,
        projectName: THE_ONE_PROJECT,
        remainingSources: snapshot.sources.filter(
          (item) => item.public_source_reference !== source.public_source_reference
        ),
        modelsExist: snapshot.registries.some((registry) => registry.entries.length > 0)
          || snapshot.configurations.some(
            (configuration) =>
              configuration.startable
              && snapshot.registries.every((registry) => registry.provider_id !== configuration.provider_id)
          )
      })
    };
  }

  function openRenewSheet(source: ProjectSourceResource): void {
    if (sourceBusy) return;
    sourceError = null;
    sourceSheet = { kind: "renew", source };
  }

  function closeSourceSheet(): void {
    if (sourceBusy) return;
    sourceSheet = null;
    sourceError = null;
  }

  async function submitConnect(address: string, token: string): Promise<void> {
    const snapshot = settings.confirmed;
    if (snapshot === null || sourceBusy) return;
    const attemptedAddress = address.trim();
    lastConnectAddress = attemptedAddress;
    if (snapshot.sources.some((item) => item.address !== attemptedAddress)) {
      nameOneSourceToday();
      return;
    }
    sourceBusy = true;
    sourceError = null;
    try {
      const created = await cockpitApi.connectProjectSource(
        snapshot.projectReference,
        connectProjectSourceBody(address, token)
      );
      const sameIdentity = snapshot.sources.some(
        (item) => item.public_source_reference === created.public_source_reference
      );
      if (!sameIdentity && snapshot.sources.length > 0) {
        nameOneSourceToday();
        return;
      }
      replaceSources(
        sameIdentity
          ? snapshot.sources.map((item) =>
              item.public_source_reference === created.public_source_reference ? created : item
            )
          : [...snapshot.sources, created],
        false
      );
      duplicateSourceReference = null;
      sourceSheet = null;
    } catch (error) {
      await rememberSourceError(error, "connect", attemptedAddress);
    } finally {
      sourceBusy = false;
    }
  }

  async function submitDisconnect(publicSourceReference: string): Promise<void> {
    const snapshot = settings.confirmed;
    if (snapshot === null || sourceBusy) return;
    sourceBusy = true;
    sourceError = null;
    try {
      await cockpitApi.disconnectProjectSource(snapshot.projectReference, publicSourceReference);
      replaceSources(
        snapshot.sources.filter((item) => item.public_source_reference !== publicSourceReference),
        false
      );
      if (duplicateSourceReference === publicSourceReference) duplicateSourceReference = null;
      sourceSheet = null;
    } catch (error) {
      await rememberSourceError(error, "disconnect");
    } finally {
      sourceBusy = false;
    }
  }

  async function submitRenew(publicSourceReference: string, token: string): Promise<void> {
    const snapshot = settings.confirmed;
    if (snapshot === null || sourceBusy) return;
    sourceBusy = true;
    sourceError = null;
    try {
      const rotated = await cockpitApi.rotateProjectSourceToken(
        snapshot.projectReference,
        publicSourceReference,
        rotateProjectSourceTokenBody(token)
      );
      replaceSources(
        snapshot.sources.map((item) =>
          item.public_source_reference === publicSourceReference ? rotated : item
        )
      );
      sourceSheet = null;
    } catch (error) {
      await rememberSourceError(error, "renew");
    } finally {
      sourceBusy = false;
    }
  }

  function rememberRegistryFailure(
    providerId: string,
    write: ExactModelRegistryRevisionWrite,
    intent: RegistryIntent,
    outcome: Exclude<RegistryPublish, { kind: "ok" }>
  ): void {
    failedWrite = {
      kind: "registry",
      providerId,
      write: outcome.kind === "uncertain" ? outcome.write : write,
      intent,
      rebase: outcome.kind === "conflict"
    };
  }

  async function rebaseRegistry(
    providerId: string,
    intent: RegistryIntent
  ): Promise<RegistryPublish> {
    try {
      const current = await cockpitApi.getModelRegistry(providerId);
      const rebased = rebasedRegistryWrite(current, intent);
      if (rebased.kind === "already-true") {
        updateRegistry(current);
        return { kind: "ok" };
      }
      try {
        const result = await cockpitApi.putModelRegistry(providerId, rebased.write);
        updateRegistry(result.value);
        return { kind: "ok" };
      } catch (error) {
        if (problemCode(error) === "model-registry-revision-conflict") {
          return { kind: "conflict" };
        }
        return { kind: "uncertain", write: rebased.write };
      }
    } catch {
      return { kind: "conflict" };
    }
  }

  async function publishRegistry(
    providerId: string,
    write: ExactModelRegistryRevisionWrite,
    intent: RegistryIntent,
    rebaseFirst = false
  ): Promise<RegistryPublish> {
    if (rebaseFirst) return rebaseRegistry(providerId, intent);
    try {
      const result = await cockpitApi.putModelRegistry(providerId, write);
      updateRegistry(result.value);
      return { kind: "ok" };
    } catch (error) {
      if (problemCode(error) === "model-registry-revision-conflict") {
        return rebaseRegistry(providerId, intent);
      }
      return { kind: "uncertain", write };
    }
  }

  async function sendRegistryWrite(
    providerId: string,
    write: ExactModelRegistryRevisionWrite,
    intent: RegistryIntent,
    retrying = false,
    rebaseFirst = false
  ): Promise<void> {
    if (writing || (failedWrite !== null && !retrying)) return;
    writing = true;
    try {
      const outcome = await publishRegistry(providerId, write, intent, rebaseFirst);
      if (outcome.kind === "ok") failedWrite = null;
      else rememberRegistryFailure(providerId, write, intent, outcome);
    } finally {
      writing = false;
    }
  }

  async function removeModel(
    registry: ModelRegistryRevision,
    entry: ModelRegistryRevision["entries"][number]
  ): Promise<void> {
    if (writing || failedWrite !== null) return;
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    unmarkChecking(entry.agent_configuration_revision_hash);
    const intent: RegistryIntent = {
      kind: "remove",
      configurationHash: entry.agent_configuration_revision_hash
    };
    const input = {
      revision_number: registry.revision_number + 1,
      entries: registry.entries
        .filter(
          (candidate) => candidate.agent_configuration_revision_hash !== entry.agent_configuration_revision_hash
        )
        .map(registryEntryInput)
    };
    const registryWrite = { input, body: JSON.stringify(input) };
    const defaultsWrite = defaultsAfterRemovingConfigurationHash(
      snapshot.defaults,
      entry.agent_configuration_revision_hash
    );
    writing = true;
    try {
      if (defaultsWrite !== null) {
        try {
          const result = await cockpitApi.putProjectModelDefaults(
            snapshot.projectReference,
            defaultsWrite
          );
          updateDefaults(result.value);
        } catch {
          failedWrite = {
            kind: "remove",
            projectReference: snapshot.projectReference,
            defaultsWrite,
            providerId: registry.provider_id,
            registryWrite,
            intent
          };
          return;
        }
      }
      const outcome = await publishRegistry(registry.provider_id, registryWrite, intent);
      if (outcome.kind === "ok") failedWrite = null;
      else rememberRegistryFailure(registry.provider_id, registryWrite, intent, outcome);
    } finally {
      writing = false;
    }
  }

  function registryEntryInput(entry: ModelRegistryRevision["entries"][number]) {
    return {
      model_id: entry.model_id,
      agent_configuration_revision_hash: entry.agent_configuration_revision_hash
    };
  }

  function additionWrite(configuration: AgentConfigurationRevisionListItem): ExactModelRegistryRevisionWrite {
    const registry = registryFor(configuration.provider_id);
    const entries = [
      ...(registry?.entries ?? []).map(registryEntryInput),
      {
        model_id: configuration.model,
        agent_configuration_revision_hash: configuration.agent_configuration_revision_hash
      }
    ];
    const input = { revision_number: (registry?.revision_number ?? 0) + 1, entries };
    return { input, body: JSON.stringify(input) };
  }

  async function fetchValidation(
    providerId: string,
    configurationHash: string
  ): Promise<{ kind: "ok"; registry: ModelRegistryRevision } | { kind: "uncertain" }> {
    try {
      const result = await cockpitApi.validateModelRegistryEntry(providerId, configurationHash);
      return { kind: "ok", registry: result.value };
    } catch {
      return { kind: "uncertain" };
    }
  }

  async function validateModel(
    providerId: string,
    configurationHash: string,
    retrying = false,
    publish: {
      write: ExactModelRegistryRevisionWrite;
      intent: RegistryIntent;
      rebaseFirst?: boolean;
      published?: PublishedAdd | null;
    } | null = null
  ): Promise<void> {
    if (writing || (failedWrite !== null && !retrying)) return;
    writing = true;
    try {
      if (entryFor(configurationHash) === null) {
        const configuration = settings.confirmed?.configurations.find(
          (candidate) =>
            candidate.agent_configuration_revision_hash === configurationHash
            && candidate.provider_id === providerId
        );
        const missingStartableRegistry = registryFor(providerId) === null
          && (settings.confirmed?.configurations.some(
            (candidate) => candidate.provider_id === providerId && candidate.startable
          ) ?? false);
        if (publish === null && !missingStartableRegistry) {
          failedWrite = null;
          return;
        }
        let write: ExactModelRegistryRevisionWrite;
        let intent: RegistryIntent;
        if (publish !== null) {
          write = publish.write;
          intent = publish.intent;
        } else {
          if (configuration === undefined) return;
          write = additionWrite(configuration);
          intent = {
            kind: "add",
            modelId: configuration.model,
            configurationHash
          };
        }
        const published = await publishRegistry(
          providerId,
          write,
          intent,
          publish?.rebaseFirst ?? false
        );
        if (published.kind !== "ok") {
          failedWrite = {
            kind: "check-publish",
            providerId,
            write: published.kind === "uncertain" ? published.write : write,
            configurationHash,
            intent,
            rebase: published.kind === "conflict",
            published: publish?.published ?? null
          };
          return;
        }
        if (publish?.published !== undefined && publish.published !== null) {
          appendConfiguration(publish.published.revision, publish.published.pin);
          closeSheet();
        }
      }
      const found = entryFor(configurationHash);
      if (found === null || found.entry.provider_check !== "not-checked") {
        failedWrite = null;
        return;
      }
      const outcome = await fetchValidation(providerId, configurationHash);
      if (outcome.kind === "uncertain") {
        failedWrite = { kind: "validation", providerId, configurationHash };
      } else {
        updateRegistry(outcome.registry);
        failedWrite = null;
      }
    } finally {
      writing = false;
    }
  }

  function markChecking(configurationHash: string): void {
    const next = new SvelteSet(checkingHashes);
    next.add(configurationHash);
    checkingHashes = next;
  }

  function unmarkChecking(configurationHash: string): void {
    if (!checkingHashes.has(configurationHash)) return;
    const next = new SvelteSet(checkingHashes);
    next.delete(configurationHash);
    checkingHashes = next;
  }

  async function checkPublished(
    providerId: string,
    configurationHash: string
  ): Promise<void> {
    markChecking(configurationHash);
    try {
      const outcome = await fetchValidation(providerId, configurationHash);
      if (!checkingHashes.has(configurationHash)) return;
      if (outcome.kind === "ok") {
        updateRegistry(outcome.registry);
        failedWrite = null;
      } else failedWrite = { kind: "validation", providerId, configurationHash };
    } finally {
      unmarkChecking(configurationHash);
    }
  }

  async function publishCreatedModel(
    profile: AuthProfileRevision,
    input: AgentConfigurationInput,
    registryWrite: (newHash: string) => ExactModelRegistryRevisionWrite,
    pin: AgentConfigurationRevisionListItem
  ): Promise<void> {
    writing = true;
    let publishedHash: string | null = null;
    try {
      const published = await cockpitApi.publishAgentConfiguration(input);
      const revision = published.value;
      const configurationHash = revision.agent_configuration_revision_hash;
      const write = registryWrite(configurationHash);
      const intent: RegistryIntent = {
        kind: "add",
        modelId: input.model,
        configurationHash
      };
      const publishedRegistry = await publishRegistry(profile.provider_id, write, intent);
      if (publishedRegistry.kind !== "ok") {
        failedWrite = {
          kind: "check-publish",
          providerId: profile.provider_id,
          write: publishedRegistry.kind === "uncertain" ? publishedRegistry.write : write,
          configurationHash,
          intent,
          rebase: publishedRegistry.kind === "conflict",
          published: { revision, pin }
        };
        return;
      }
      appendConfiguration(revision, pin);
      failedWrite = null;
      closeSheet();
      publishedHash = configurationHash;
    } catch {
      failedWrite = { kind: "configuration", input };
      return;
    } finally {
      writing = false;
    }
    if (publishedHash === null) return;
    await checkPublished(profile.provider_id, publishedHash);
  }

  async function submitAdd(profile: AuthProfileRevision, modelId: string): Promise<void> {
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    const plan = planAddModel({
      modelId: trimmedModelId(modelId),
      profile,
      configurations: snapshot.configurations,
      registries: snapshot.registries
    });
    if (plan.kind === "invalid-id" || plan.kind === "no-pin") return;
    if (plan.kind === "duplicate") {
      duplicateNotice = { providerId: profile.provider_id, modelId: plan.entry.model_id };
      closeSheet();
      return;
    }
    if (writing || failedWrite !== null) return;
    duplicateNotice = null;
    await publishCreatedModel(profile, plan.input, plan.registryWrite, plan.pin);
  }

  async function retryConfiguration(input: AgentConfigurationInput): Promise<void> {
    const snapshot = settings.confirmed;
    if (snapshot === null || writing) return;
    const profile = snapshot.profiles.find(
      (candidate) => candidate.auth_profile_revision_hash === input.auth_profile_revision_hash
    );
    if (profile === undefined) return;
    const plan = planAddModel({
      modelId: input.model,
      profile,
      configurations: snapshot.configurations,
      registries: snapshot.registries
    });
    if (plan.kind !== "create") {
      failedWrite = null;
      closeSheet();
      return;
    }
    await publishCreatedModel(profile, input, plan.registryWrite, plan.pin);
  }

  function updateDefaults(result: ProjectModelDefaultsRevision): void {
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    settings = { ...settings, confirmed: { ...snapshot, defaults: result } };
    selections = Object.fromEntries(
      result.defaults.map((item) => [item.difficulty, item.agent_configuration_revision_hash])
    );
  }

  async function sendDefaultsWrite(
    projectReference: string,
    write: ExactProjectModelDefaultsRevisionWrite,
    retrying = false
  ): Promise<void> {
    if (writing || (failedWrite !== null && !retrying)) return;
    writing = true;
    try {
      const result = await cockpitApi.putProjectModelDefaults(projectReference, write);
      updateDefaults(result.value);
      failedWrite = null;
    } catch {
      if (!retrying) failedWrite = { kind: "defaults", projectReference, write };
    } finally {
      writing = false;
    }
  }

  function defaultReplacement(
    difficulty: Difficulty,
    found: NonNullable<ReturnType<typeof entryFor>>
  ) {
    return {
      difficulty,
      model_registry_revision_hash: found.registry.model_registry_revision_hash,
      provider_id: found.registry.provider_id,
      model_id: found.entry.model_id,
      agent_configuration_revision_hash: found.entry.agent_configuration_revision_hash
    };
  }

  async function choose(difficulty: Difficulty, configurationHash: string): Promise<void> {
    if (writing || failedWrite !== null) return;
    const snapshot = settings.confirmed;
    if (snapshot === null) return;
    const untouched = snapshot.defaults?.defaults.filter(
      (item) => item.difficulty !== difficulty
    ) ?? [];
    let defaults = untouched;
    if (configurationHash !== "") {
      const found = entryFor(configurationHash);
      if (found === null || found.entry.provider_check !== "checked") return;
      const configuration = snapshot.configurations.find(
        (candidate) => candidate.agent_configuration_revision_hash === configurationHash
      );
      if (configuration === undefined || !configuration.startable) return;
      defaults = [...untouched, defaultReplacement(difficulty, found)];
    }
    const input = { revision_number: (snapshot.defaults?.revision_number ?? 0) + 1, defaults };
    await sendDefaultsWrite(snapshot.projectReference, { input, body: JSON.stringify(input) });
  }

  async function retryRemove(
    retry: Extract<FailedWrite, { kind: "remove" }>
  ): Promise<void> {
    if (writing) return;
    writing = true;
    try {
      try {
        const result = await cockpitApi.putProjectModelDefaults(
          retry.projectReference,
          retry.defaultsWrite
        );
        updateDefaults(result.value);
      } catch {
        return;
      }
      const outcome = await publishRegistry(
        retry.providerId,
        retry.registryWrite,
        retry.intent
      );
      if (outcome.kind === "ok") failedWrite = null;
      else rememberRegistryFailure(retry.providerId, retry.registryWrite, retry.intent, outcome);
    } finally {
      writing = false;
    }
  }

  async function retryWrite(): Promise<void> {
    const retry = failedWrite;
    if (retry === null) return;
    if (retry.kind === "registry") {
      await sendRegistryWrite(
        retry.providerId,
        retry.write,
        retry.intent,
        true,
        retry.rebase
      );
    } else if (retry.kind === "remove") {
      await retryRemove(retry);
    } else if (retry.kind === "check-publish") {
      await validateModel(retry.providerId, retry.configurationHash, true, {
        write: retry.write,
        intent: retry.intent,
        rebaseFirst: retry.rebase,
        published: retry.published
      });
    } else if (retry.kind === "validation") {
      await checkPublished(retry.providerId, retry.configurationHash);
    } else if (retry.kind === "configuration") {
      await retryConfiguration(retry.input);
    } else await sendDefaultsWrite(retry.projectReference, retry.write, true);
  }

  $: registeredChoices = (settings.confirmed?.registries ?? []).flatMap((registry) =>
    registry.entries.flatMap((entry) => {
      if (entry.provider_check !== "checked") return [];
      const configuration = settings.confirmed?.configurations.find(
        (candidate) => candidate.agent_configuration_revision_hash === entry.agent_configuration_revision_hash
      );
      if (configuration === undefined || !configuration.startable) return [];
      return [{
        value: entry.agent_configuration_revision_hash,
        label: accountChoice(entry.model_id, accountFor(configuration))
      }];
    })
  );

  function retainedDefaultChoice(configurationHash: string): { value: string; label: string } | null {
    if (registeredChoices.some((choice) => choice.value === configurationHash)) return null;
    const found = entryFor(configurationHash);
    if (found === null) return { value: configurationHash, label: settingsPageCopy.unavailableSavedModel };
    const configuration = settings.confirmed?.configurations.find(
      (candidate) => candidate.agent_configuration_revision_hash === configurationHash
    );
    return {
      value: configurationHash,
      label: retainedAccountChoice(
        found.entry.model_id,
        configuration === undefined ? settingsPageCopy.unknownAccount : accountFor(configuration)
      )
    };
  }

  $: missingRegistryStartable = (settings.confirmed?.configurations ?? []).filter(
    (configuration) => configuration.startable && registryFor(configuration.provider_id) === null
  );
  $: registryEntries = (settings.confirmed?.registries ?? []).flatMap(
    (registry) => registry.entries
  );
  $: checkableEntries = registryEntries.filter(
    (entry) => entry.provider_check === "not-checked"
  );
  $: listedModelCount = registryEntries.length + missingRegistryStartable.length;
  $: modelProviders = [...new Set([
    ...(settings.confirmed?.registries ?? [])
      .filter((registry) => registry.entries.length > 0)
      .map((registry) => registry.provider_id),
    ...missingRegistryStartable.map((configuration) => configuration.provider_id)
  ])].sort();
  $: checkingList = [...checkingHashes];
  $: modelGroups = modelProviders.map((providerId) => {
    const registry = registryFor(providerId);
    if (registry === null) {
      const missing = missingRegistryStartable.filter(
        (configuration) => configuration.provider_id === providerId
      );
      return {
        providerId,
        registry: null,
        rows: [],
        missing,
        caption: providerId
      };
    }
    const rows = registry.entries.map((entry) => {
      const configuration = settings.confirmed?.configurations.find(
        (candidate) => candidate.agent_configuration_revision_hash === entry.agent_configuration_revision_hash
      );
      return {
        entry,
        configuration,
        presentation: rowPresentation(entry, checkingList.includes(entry.agent_configuration_revision_hash))
      };
    });
    return {
      providerId,
      registry,
      missing: [],
      rows,
      caption: providerId
    };
  });
</script>

<section class="settings-page" aria-labelledby="settings-title">
  <header><h1 id="settings-title">{THE_ONE_PROJECT}</h1></header>

  <ReadState read={settings} label={settingsPageCopy.label} onRetry={() => { void load(); }} />

  {#if settings.confirmed !== null}
    <section class="settings-block" aria-labelledby="sources-title">
      <h2 id="sources-title">{wrapDisplayCopy(settingsPageCopy.sourcesTitle)}</h2>
      {#if settings.confirmed.severalNotBuilt}
        <p class="source-limit" role="status">{settingsPageCopy.oneSourceToday}</p>
      {/if}
      {#if sourceRows.length > 0}
        <ul class="source-rows">
          {#each sourceRows as row (row.publicSourceReference)}
            <li class="source-row">
              <span class="source-chip">{row.chip}</span>
              <div class="source-detail">
                <strong>{row.headline}</strong>
                <span> · {row.scope} · {row.connected}</span>
                {#if row.duplicate}
                  <span class="source-duplicate">{sourceAlreadyPresent(row.address)}</span>
                {/if}
              </div>
              <div class="source-actions">
                <button
                  class="source-disconnect"
                  type="button"
                  disabled={sourceBusy}
                  onclick={() => {
                    const source = sourceFor(row.publicSourceReference);
                    if (source !== null) openDisconnectSheet(source);
                  }}
                >{settingsPageCopy.disconnect}</button>
                <button
                  class="source-renew"
                  type="button"
                  disabled={sourceBusy}
                  onclick={() => {
                    const source = sourceFor(row.publicSourceReference);
                    if (source !== null) openRenewSheet(source);
                  }}
                >{settingsPageCopy.renewToken}</button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
      <button class="quiet compact connect-source" type="button" disabled={sourceBusy} onclick={() => { openConnectSheet(); }}>
        {settingsPageCopy.connectASource}
      </button>
    </section>

    <ProviderAccounts rows={accountRows} />

    <section class="settings-block" aria-labelledby="models-title">
      <h2 id="models-title">{wrapDisplayCopy(settingsPageCopy.modelsTitle)}</h2>
      {#snippet addModelButton()}
        <button class="quiet compact add-model" type="button" disabled={mutationsFrozen} onclick={() => { openAddSheet(null); }}>
          {settingsPageCopy.addModel}
        </button>
      {/snippet}
      {#if listedModelCount > 0}
        <div class="table-wrap">
          <table>
            <thead><tr><th>{settingsPageCopy.model}</th><th>{settingsPageCopy.account}</th><th><span class="sr-only">{settingsPageCopy.registry}</span></th></tr></thead>
            <tbody>
              {#each modelGroups as group (group.providerId)}
                {@const registry = group.registry}
                <tr class="provider-row"><td colspan="3">{group.caption}</td></tr>
                {#if registry !== null}
                  {#each group.rows as row (`${row.entry.agent_configuration_revision_hash}:${row.presentation}`)}
                    {@const entry = row.entry}
                    {@const configuration = row.configuration}
                    {@const presentation = row.presentation}
                    <tr>
                      <td data-label={settingsPageCopy.model}><code>{entry.model_id}</code></td>
                      <td data-label={settingsPageCopy.account}>{configuration === undefined ? settingsPageCopy.unknownAccount : accountFor(configuration)}</td>
                      <td class="model-actions-cell" data-label={settingsPageCopy.registry}>
                        <div class="model-actions">
                          {#if presentation === "checking"}
                            <span class="checking">{settingsPageCopy.checking}</span>
                          {:else if presentation === "unknown"}
                            <span class="error">{noSuchModel(registry.provider_id)}</span>
                            <button class="quiet compact" type="button" disabled={mutationsFrozen} onclick={() => { openAddSheet({ authProfileRevisionHash: configuration?.auth_profile_revision_hash ?? "", modelId: entry.model_id }); }}>{settingsPageCopy.correctTheId}</button>
                          {:else if presentation === "added-checked"}
                            <span>{settingsPageCopy.addedByYouChecked}</span>
                          {:else if presentation === "added-not-checked"}
                            <span class="unchecked">{settingsPageCopy.addedByYouNotChecked}</span>
                          {/if}
                          {#if duplicateNotice !== null && duplicateNotice.providerId === registry.provider_id && duplicateNotice.modelId === entry.model_id}
                            <span>{settingsPageCopy.alreadyPresent}</span>
                          {/if}
                          {#if failedWrite?.kind === "validation" && failedWrite.configurationHash === entry.agent_configuration_revision_hash}
                            <div class="write-failure" role="alert">
                              <span aria-hidden="true">◇</span><strong>{settingsPageCopy.writeFailed}</strong>
                            </div>
                          {/if}
                          {#if presentation !== "checking" && entry.provider_check === "not-checked"}
                            <button class="quiet compact" type="button" disabled={mutationsFrozen} onclick={() => { void validateModel(registry.provider_id, entry.agent_configuration_revision_hash); }}>{settingsPageCopy.check}</button>
                          {:else if presentation !== "checking" && entry.provider_check === "checked" && entry.source === "operator"}
                            <button class="quiet compact" type="button" disabled={mutationsFrozen} onclick={() => { void checkPublished(registry.provider_id, entry.agent_configuration_revision_hash); }}>{settingsPageCopy.check}</button>
                          {/if}
                          <button class="quiet compact" type="button" disabled={mutationsFrozen} onclick={() => { void removeModel(registry, entry); }}>{settingsPageCopy.remove}</button>
                        </div>
                      </td>
                    </tr>
                  {/each}
                {:else}
                  {#each group.missing as configuration (configuration.agent_configuration_revision_hash)}
                    <tr>
                      <td data-label={settingsPageCopy.model}><code>{configuration.model}</code></td>
                      <td data-label={settingsPageCopy.account}>{accountFor(configuration)}</td>
                      <td class="model-actions-cell" data-label={settingsPageCopy.registry}>
                        <div class="model-actions">
                          <span class="unchecked">{settingsPageCopy.notCheckedYet}</span>
                          <button class="quiet compact" type="button" disabled={mutationsFrozen} onclick={() => { void validateModel(configuration.provider_id, configuration.agent_configuration_revision_hash); }}>{settingsPageCopy.check}</button>
                        </div>
                      </td>
                    </tr>
                  {/each}
                {/if}
              {/each}
              <tr class="add-model-row"><td colspan="3">{@render addModelButton()}</td></tr>
            </tbody>
          </table>
        </div>
      {:else}
        {@render addModelButton()}
      {/if}
    </section>

    <section class="settings-block" aria-labelledby="defaults-title">
      <h2 id="defaults-title">{wrapDisplayCopy(settingsPageCopy.defaultsTitle)}</h2>
      {#if registeredChoices.length === 0 && listedModelCount > 0}
        <div class="empty-state" role="status"><span aria-hidden="true">◇</span><strong>{checkableEntries.length > 0 || missingRegistryStartable.length > 0 ? settingsPageCopy.defaultsNoCheckedModels : settingsPageCopy.defaultsUnavailableModels}</strong></div>
      {/if}
      <div class="table-wrap">
        <table class="defaults-table">
          <thead><tr><th>{settingsPageCopy.difficulty}</th><th>{settingsPageCopy.model}</th></tr></thead>
          <tbody>
          {#each difficulties as difficulty (difficulty)}
            {@const retained = selections[difficulty] === undefined ? null : retainedDefaultChoice(selections[difficulty])}
            <tr>
              <td class="difficulty-mark" data-label={settingsPageCopy.difficulty}><b>{difficulty}</b></td>
              <td data-label={settingsPageCopy.model}>
              {#if retained !== null}
                <div class="retained-default" role="status">{retained.label}</div>
              {/if}
              <select
                aria-label={difficultyLabel(difficulty)}
                value={retained === null ? selections[difficulty] ?? "" : ""}
                disabled={mutationsFrozen}
                onchange={(event) => { void choose(difficulty, event.currentTarget.value === "__clear" ? "" : event.currentTarget.value); }}
              >
                {#if retained !== null}
                  <option value="" disabled>{settingsPageCopy.changeSavedDefault}</option>
                  <option value="__clear">{settingsPageCopy.noDefault}</option>
                {:else}
                  <option value="">{settingsPageCopy.noDefault}</option>
                {/if}
                {#each registeredChoices as choice (choice.value)}
                  <option value={choice.value}>{choice.label}</option>
                {/each}
              </select>
              </td>
            </tr>
          {/each}
          </tbody>
        </table>
      </div>
    </section>

    {#if failedWrite !== null}
      <div class="write-failure" role={failedWrite.kind === "validation" ? undefined : "alert"}>
        {#if failedWrite.kind !== "validation"}
          <span aria-hidden="true">◇</span><strong>{settingsPageCopy.writeFailed}</strong>
          {#if (failedWrite.kind === "registry" || failedWrite.kind === "check-publish") && failedWrite.rebase}
            <span>{settingsPageCopy.registryChanged}</span>
          {/if}
        {/if}
        <button class="quiet compact" type="button" disabled={writing} onclick={() => { void retryWrite(); }}>{settingsPageCopy.retry}</button>
      </div>
    {/if}

    {#if sheetOpen}
      <AddModelSheet
        options={accountOptions}
        prefill={sheetPrefill}
        submitting={mutationsFrozen}
        onSubmit={(args) => { void submitAdd(args.profile, args.modelId); }}
        onClose={() => { sheetOpen = false; }}
      />
    {/if}
    {#if sourceSheet?.kind === "connect"}
      <ConnectSourceSheet
        submitting={sourceBusy}
        error={sourceError}
        onSubmit={(args) => { void submitConnect(args.address, args.token); }}
        onRetry={() => { void retryDuplicateSource(); }}
        onClose={() => { closeSourceSheet(); }}
      />
    {/if}
    {#if sourceSheet?.kind === "disconnect"}
      <DisconnectSourceSheet
        facts={sourceSheet.facts}
        submitting={sourceBusy}
        error={sourceError}
        onConfirm={() => {
          if (sourceSheet?.kind !== "disconnect") return;
          void submitDisconnect(sourceSheet.source.public_source_reference);
        }}
        onClose={() => { closeSourceSheet(); }}
      />
    {/if}
    {#if sourceSheet?.kind === "renew"}
      <RenewSourceTokenSheet
        submitting={sourceBusy}
        error={sourceError}
        onSubmit={(args) => {
          if (sourceSheet?.kind !== "renew") return;
          void submitRenew(sourceSheet.source.public_source_reference, args.token);
        }}
        onClose={() => { closeSourceSheet(); }}
      />
    {/if}
  {/if}
</section>

<style>
  .settings-page, .settings-block { display: grid; align-content: start; gap: var(--space-3); min-width: 0; }
  .settings-page { gap: var(--space-section); }
  h1, h2 { margin: 0; }
  h2 { color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .source-rows { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
  .source-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: var(--space-3); align-items: center; background: var(--panel2); border: var(--edge) solid var(--line); border-radius: var(--r-lg); padding: var(--space-3); }
  .source-chip { color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .source-detail { min-width: 0; color: var(--ink-dim); overflow-wrap: anywhere; }
  .source-detail strong { color: var(--ink); font-weight: var(--weight-strong); }
  .source-actions { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; justify-content: flex-end; }
  .source-disconnect { min-height: var(--tap); border-color: transparent; background: transparent; color: var(--signal-failure); text-decoration: none; font-weight: var(--weight-medium); }
  .source-renew { min-height: var(--tap); border-color: transparent; background: transparent; color: var(--ink); text-decoration: underline; text-underline-offset: var(--underline-offset); font-weight: var(--weight-strong); }
  .source-duplicate { margin-left: var(--space-2); }
  .source-limit { margin: 0; color: var(--ink-dim); }
  .connect-source { display: block; width: fit-content; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; min-width: var(--table-min); width: 100%; }
  th, td { border-bottom: var(--edge) solid var(--line); padding: var(--space-3); text-align: left; white-space: nowrap; }
  th { color: var(--ink-dim); font-size: var(--text-2xs); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  td:last-child, th:last-child { text-align: right; }
  .provider-row td { background: var(--panel2); color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-align: left; text-transform: uppercase; }
  .add-model { display: block; width: fit-content; }
  .add-model-row td { text-align: left; }
  .model-actions-cell { white-space: normal; }
  .model-actions { display: flex; flex-wrap: wrap; gap: var(--space-2); justify-content: flex-end; align-items: center; }
  .unchecked { color: var(--signal-attention); }
  .checking { color: var(--signal-live); }
  .error { color: var(--signal-failure); }
  .difficulty-mark { font-size: var(--text-lg); font-weight: var(--weight-heavy); }
  .defaults-table { min-width: 0; }
  .defaults-table td:last-child { text-align: left; }
  .defaults-table select { padding-inline: var(--space-2); }
  .retained-default { margin-bottom: var(--space-2); max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
  .empty-state, .write-failure { display: flex; gap: var(--space-2); align-items: center; color: var(--ink-dim); }
  .write-failure { color: var(--signal-failure); }
  .write-failure button { margin-left: auto; }
  .compact { min-height: var(--tap); padding: var(--space-1) var(--space-3); }
  @media (max-width: 520px) {
    .source-row { grid-template-columns: auto minmax(0, 1fr); }
    .source-actions { grid-column: 1 / -1; justify-content: flex-start; }
    table { display: block; min-width: 0; }
    thead { display: none; }
    tbody { display: grid; gap: var(--space-3); }
    tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border: var(--edge) solid var(--line); border-radius: var(--r-lg); padding: var(--space-2); background: var(--panel2); }
    td { border: 0; padding: var(--space-2); white-space: normal; overflow-wrap: anywhere; }
    td::before { content: attr(data-label); display: block; margin-bottom: var(--space-1); color: var(--ink-dim); font-size: var(--text-2xs); letter-spacing: var(--tracking-label); text-transform: uppercase; }
    td:first-child, td:last-child { grid-column: 1 / -1; }
    td:last-child { text-align: left; }
    .provider-row { display: table-row; border: 0; border-radius: 0; padding: 0; background: transparent; }
    .provider-row td { display: table-cell; padding: var(--space-2); }
    .add-model-row td::before { content: none; display: none; }
    .defaults-table tr { grid-template-columns: minmax(var(--tap), auto) minmax(0, 1fr); }
    .defaults-table td:first-child { grid-column: auto; }
    .defaults-table select { width: 100%; max-width: 100%; font-size: var(--text-sm); }
  }
</style>
