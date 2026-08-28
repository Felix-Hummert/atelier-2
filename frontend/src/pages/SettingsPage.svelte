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
    type ProjectSourceConnectionRevision
  } from "../api/client";
  import AddModelSheet from "../components/AddModelSheet.svelte";
  import ReadState from "../components/ReadState.svelte";
  import {
    offeredAccounts,
    planAddModel,
    rowPresentation,
    trimmedModelId
  } from "../lib/addModel";
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
  import { readEveryAgentConfiguration } from "../lib/runPages";
  import {
    accountChoice,
    difficultyLabel,
    noSuchModel,
    retainedAccountChoice,
    settingsPageCopy
  } from "../lib/settingsPageCopy";

  export let cockpitApi: CockpitApi;

  const difficulties = [3, 2, 1] as const;
  type Difficulty = typeof difficulties[number];
  type SettingsFailure = { kind: "unavailable"; title: string };

  interface SettingsSnapshot {
    projectReference: string;
    source: ProjectSourceConnectionRevision | null;
    configurations: AgentConfigurationRevisionListItem[];
    profiles: AuthProfileRevision[];
    registries: ModelRegistryRevision[];
    defaults: ProjectModelDefaultsRevision | null;
  }

  type FailedWrite =
    | {
      kind: "registry";
      providerId: string;
      write: ExactModelRegistryRevisionWrite;
      }
    | {
        kind: "check-publish";
        providerId: string;
        write: ExactModelRegistryRevisionWrite;
        configurationHash: string;
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

  type SheetPrefill = { authProfileRevisionHash: string; modelId: string };

  let settings: RetainedRead<SettingsSnapshot, SettingsFailure> = retainedRead();
  let selections: Partial<Record<Difficulty, string>> = {};
  let writing = false;
  let failedWrite: FailedWrite | null = null;
  let sheetOpen = false;
  let sheetPrefill: SheetPrefill | null = null;
  let checkingHashes = new SvelteSet<string>();
  let duplicateNotice: { providerId: string; modelId: string } | null = null;

  $: mutationsFrozen = writing || failedWrite !== null;
  $: accountOptions = settings.confirmed === null
    ? []
    : offeredAccounts(settings.confirmed.profiles, settings.confirmed.configurations);

  onMount(() => { void load(); });

  async function readProfiles(): Promise<AuthProfileRevision[]> {
    const profiles: AuthProfileRevision[] = [];
    let after: string | undefined;
    const followed = new SvelteSet<string>();
    for (;;) {
      const page = await cockpitApi.listAuthProfileRevisions(after);
      profiles.push(...page.items);
      if (page.next_after_revision_hash === null) return profiles;
      if (followed.has(page.next_after_revision_hash)) throw new Error("profile cursor repeated");
      followed.add(page.next_after_revision_hash);
      after = page.next_after_revision_hash;
    }
  }

  async function load(): Promise<void> {
    const begun = beginRead(settings);
    settings = begun.read;
    failedWrite = null;
    try {
      const projects = await cockpitApi.listProjects();
      const projectReference = projects.items[0]?.public_project_reference;
      if (projectReference === undefined) throw new Error("served project missing");

      const [source, configurationReading, profiles] = await Promise.all([
        cockpitApi.getProjectSourceConnection(projectReference).catch((error: unknown) => {
          if (problemCode(error) === "project-source-not-connected") return null;
          throw error;
        }),
        readEveryAgentConfiguration((after) => cockpitApi.listAgentConfigurationRevisions(after)),
        readProfiles()
      ]);
      if (!configurationReading.complete) throw new Error("model listing incomplete");

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
      selections = Object.fromEntries(
        (defaults?.defaults ?? []).map((item) => [
          item.difficulty,
          item.agent_configuration_revision_hash
        ])
      );
      settings = confirmRead(settings, begun.generation, {
        projectReference,
        source,
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

  async function publishRegistry(
    providerId: string,
    write: ExactModelRegistryRevisionWrite
  ): Promise<"ok" | "uncertain"> {
    try {
      const result = await cockpitApi.putModelRegistry(providerId, write);
      updateRegistry(result.value);
      return "ok";
    } catch {
      return "uncertain";
    }
  }

  async function sendRegistryWrite(
    providerId: string,
    write: ExactModelRegistryRevisionWrite,
    retrying = false
  ): Promise<void> {
    if (writing || (failedWrite !== null && !retrying)) return;
    writing = true;
    try {
      const outcome = await publishRegistry(providerId, write);
      if (outcome === "ok") failedWrite = null;
      else if (!retrying) failedWrite = { kind: "registry", providerId, write };
    } finally {
      writing = false;
    }
  }

  async function removeModel(
    registry: ModelRegistryRevision,
    entry: ModelRegistryRevision["entries"][number]
  ): Promise<void> {
    unmarkChecking(entry.agent_configuration_revision_hash);
    const input = {
      revision_number: registry.revision_number + 1,
      entries: registry.entries
        .filter(
          (candidate) => candidate.agent_configuration_revision_hash !== entry.agent_configuration_revision_hash
        )
        .map(registryEntryInput)
    };
    await sendRegistryWrite(registry.provider_id, { input, body: JSON.stringify(input) });
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
    exactPublish: ExactModelRegistryRevisionWrite | null = null
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
        if (exactPublish === null && !missingStartableRegistry) {
          failedWrite = null;
          return;
        }
        if (configuration === undefined) return;
        const write = exactPublish ?? additionWrite(configuration);
        const published = await publishRegistry(providerId, write);
        if (published === "uncertain") {
          failedWrite = { kind: "check-publish", providerId, write, configurationHash };
          return;
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
      if (outcome.kind === "ok") updateRegistry(outcome.registry);
      else failedWrite = { kind: "validation", providerId, configurationHash };
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
      appendConfiguration(revision, pin);
      const write = registryWrite(revision.agent_configuration_revision_hash);
      const publishedRegistry = await publishRegistry(profile.provider_id, write);
      if (publishedRegistry === "uncertain") {
        failedWrite = {
          kind: "check-publish",
          providerId: profile.provider_id,
          write,
          configurationHash: revision.agent_configuration_revision_hash
        };
        return;
      }
      failedWrite = null;
      closeSheet();
      publishedHash = revision.agent_configuration_revision_hash;
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

  async function retryWrite(): Promise<void> {
    const retry = failedWrite;
    if (retry === null) return;
    if (retry.kind === "registry") await sendRegistryWrite(retry.providerId, retry.write, true);
    else if (retry.kind === "check-publish") {
      await validateModel(retry.providerId, retry.configurationHash, true, retry.write);
    } else if (retry.kind === "validation") {
      await validateModel(retry.providerId, retry.configurationHash, true);
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
      return {
        providerId,
        registry: null,
        rows: [],
        missing: missingRegistryStartable.filter(
          (configuration) => configuration.provider_id === providerId
        )
      };
    }
    return {
      providerId,
      registry,
      missing: [],
      rows: registry.entries.map((entry) => {
        const configuration = settings.confirmed?.configurations.find(
          (candidate) => candidate.agent_configuration_revision_hash === entry.agent_configuration_revision_hash
        );
        return {
          entry,
          configuration,
          presentation: rowPresentation(entry, checkingList.includes(entry.agent_configuration_revision_hash))
        };
      })
    };
  });
</script>

<section class="settings-page" aria-labelledby="settings-title">
  <header><h1 id="settings-title">{THE_ONE_PROJECT}</h1></header>

  <ReadState read={settings} label={settingsPageCopy.label} onRetry={() => { void load(); }} />

  {#if settings.confirmed !== null}
    <section class="settings-block" aria-labelledby="sources-title">
      <h2 id="sources-title">{wrapDisplayCopy(settingsPageCopy.sourcesTitle)}</h2>
      {#if settings.confirmed.source === null}
        <p class="muted">{wrapDisplayCopy(settingsPageCopy.sourcesEmpty)}</p>
      {:else}
        <dl class="source-list">
          <div><dt>{settingsPageCopy.sourceKind}</dt><dd>{settings.confirmed.source.source_kind}</dd></div>
          <div><dt>{settingsPageCopy.sourceAddress}</dt><dd>{settings.confirmed.source.source_address}</dd></div>
          <div><dt>{settingsPageCopy.sourceAuthMethod}</dt><dd>{settings.confirmed.source.auth_method}</dd></div>
          <div><dt>{settingsPageCopy.sourceRevision}</dt><dd>{settings.confirmed.source.revision_number}</dd></div>
        </dl>
      {/if}
    </section>

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
                <tr class="provider-row"><td colspan="3">{group.providerId}</td></tr>
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
                          {#if presentation !== "checking" && entry.provider_check === "not-checked"}
                            <button class="quiet compact" type="button" disabled={mutationsFrozen} onclick={() => { void validateModel(registry.provider_id, entry.agent_configuration_revision_hash); }}>{settingsPageCopy.check}</button>
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
      {#if registeredChoices.length === 0}
        <div class="empty-state" role="status"><span aria-hidden="true">◇</span><strong>{listedModelCount === 0 ? settingsPageCopy.defaultsEmptyRegistry : checkableEntries.length > 0 || missingRegistryStartable.length > 0 ? settingsPageCopy.defaultsNoCheckedModels : settingsPageCopy.defaultsUnavailableModels}</strong></div>
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
      <div class="write-failure" role="alert">
        <span aria-hidden="true">◇</span><strong>{settingsPageCopy.writeFailed}</strong>
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
  {/if}
</section>

<style>
  .settings-page, .settings-block { display: grid; align-content: start; gap: var(--space-3); min-width: 0; }
  .settings-page { gap: var(--space-section); }
  h1, h2 { margin: 0; }
  h2 { color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .muted { color: var(--ink-dim); }
  .source-list { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-3); margin: 0; }
  .source-list div { border: var(--edge) solid var(--line); border-radius: var(--r-lg); padding: var(--space-3); background: var(--panel2); }
  dt { color: var(--ink-dim); font-size: var(--text-2xs); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  dd { margin: var(--space-1) 0 0; overflow-wrap: anywhere; }
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
    .source-list { grid-template-columns: 1fr; }
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
