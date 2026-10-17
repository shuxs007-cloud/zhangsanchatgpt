// Pure shared D/E business-receipt validation. No network, coercion or retries.
export function validateExpiryReceipt(response, batchCards, { allowNotFound = false } = {}) {
  const fail = reason => { throw new Error(`到期标记业务回执不完整: ${reason}`); };
  if (!Array.isArray(batchCards) || !batchCards.length
      || batchCards.some(card => typeof card !== 'string' || !/^[0-9]{10}$/.test(card))
      || new Set(batchCards).size !== batchCards.length) fail('输入号码无效或重复');
  const data = response?.data;
  if (response?.code !== 200 || !data || typeof data !== 'object' || Array.isArray(data)) fail('缺少同步成功事务');
  const { inputCount, matchedCardCount, updatedChatCount } = data;
  if (![inputCount, matchedCardCount, updatedChatCount].every(n => Number.isSafeInteger(n) && n >= 0)) fail('计数必须为非负安全整数');
  if (!Array.isArray(data.notFound)) fail('缺少明确的未命中列表');
  const notFound = data.notFound.map(card => {
    if (typeof card !== 'string' || !/^(?:1)?[0-9]{10}$/.test(card)) fail('未命中号码无效');
    return card.length === 11 ? card.slice(1) : card;
  });
  const missing = new Set(notFound);
  if (inputCount !== batchCards.length || missing.size !== notFound.length
      || notFound.some(card => !batchCards.includes(card))
      || matchedCardCount + notFound.length !== inputCount
      || (!allowNotFound && notFound.length)) fail('输入、命中和未命中集合不一致');
  // updatedChatCount counts changed conversations, not matched phone numbers.
  // A synchronous match with zero changed rows is a valid idempotent outcome.
  return { inputCount, matchedCardCount, updatedChatCount, notFound,
    matchedCards: batchCards.filter(card => !missing.has(card)) };
}
