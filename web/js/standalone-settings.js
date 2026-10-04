// Optional tuning for the standalone editor/player only. Imported package
// settings remain mandatory whenever their manifest explicitly declares them.
export async function loadStandaloneSettings(url, fetcher = globalThis.fetch) {
  const response = await fetcher(url, { cache: 'no-cache' });
  // Backward compatibility with a plain static server. The supported editor
  // server returns an empty object with HTTP200 when optional tuning is absent.
  if (response.status === 404) return {};
  if (!response.ok) throw new Error('Failed to load standalone settings: ' + response.status);
  const value = await response.json();
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Standalone settings must be an object');
  }
  return value;
}
