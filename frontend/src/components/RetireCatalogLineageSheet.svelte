<script lang="ts">
  import ThreeFactConfirmSheet from "./ThreeFactConfirmSheet.svelte";
  import { catalogPageCopy, workflowDetailCopy } from "../lib/catalogPageCopy";

  export let name: string;
  export let submitting: boolean;
  export let failure: string | null;
  export let onConfirm: () => void;
  export let onDismiss: () => void;

  $: title = workflowDetailCopy.retireTitle(name);
</script>

<ThreeFactConfirmSheet
  ariaLabel={title}
  heading={title}
  facts={[
    { label: workflowDetailCopy.retireDisappears, text: workflowDetailCopy.retireDisappearsFact },
    { label: workflowDetailCopy.retireStays, text: workflowDetailCopy.retireStaysFact },
    { label: workflowDetailCopy.retirePermanent, text: workflowDetailCopy.retirePermanentFact }
  ]}
  confirmLabel={workflowDetailCopy.retire}
  cancelLabel={catalogPageCopy.cancel}
  {submitting}
  {failure}
  {onConfirm}
  {onDismiss}
/>
