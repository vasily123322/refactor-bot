import type { PostDocument } from './types';

function jsonEquivalent(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;

  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) {
      return false;
    }
    return left.every((value, index) => jsonEquivalent(value, right[index]));
  }

  if (
    typeof left !== 'object' ||
    left === null ||
    typeof right !== 'object' ||
    right === null
  ) {
    return false;
  }

  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord)
    .filter((key) => leftRecord[key] !== undefined)
    .sort();
  const rightKeys = Object.keys(rightRecord)
    .filter((key) => rightRecord[key] !== undefined)
    .sort();

  if (leftKeys.length !== rightKeys.length) return false;
  for (let index = 0; index < leftKeys.length; index += 1) {
    if (leftKeys[index] !== rightKeys[index]) return false;
    const key = leftKeys[index];
    if (!jsonEquivalent(leftRecord[key], rightRecord[key])) return false;
  }
  return true;
}

export function postDocumentsEqual(left: PostDocument, right: PostDocument): boolean {
  return jsonEquivalent(left, right);
}

export function isEditorDocumentDirty(
  baseline: PostDocument,
  current: PostDocument,
): boolean {
  return !postDocumentsEqual(baseline, current);
}

export function reconcileSuccessfulDraftSave(
  saved: PostDocument,
  current: PostDocument,
): Readonly<{ baseline: PostDocument; dirty: boolean }> {
  return {
    baseline: saved,
    dirty: isEditorDocumentDirty(saved, current),
  };
}
