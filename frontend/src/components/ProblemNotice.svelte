<script lang="ts">
  export let message: string;
</script>

<div class="notice" role="alert">
  <strong>Couldn’t complete that request.</strong>
  <span>{message}</span>
</div>
