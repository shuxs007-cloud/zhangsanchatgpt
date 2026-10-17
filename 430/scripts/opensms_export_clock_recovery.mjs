// Only called before export submission, on the current owned hidden target.
export async function readExportClockBeforeSubmit({ read, reload, record }) {
  try { return await read(); } catch (error) {
    if (error?.code !== 'EXPORT_SERVER_CLOCK_MISSING') throw error;
    await record({event: 'export_clock_recovery', status: 'reload_pending', attempt: 1});
    await reload();
    // One retry only; authentication, navigation and ambiguous clocks still fail.
    const result = await read();
    await record({event: 'export_clock_recovery', status: 'verified', attempt: 1});
    return result;
  }
}
