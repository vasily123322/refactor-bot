import type { PostBlock } from './types';

export const MAX_STUDIO_NESTING_DEPTH = 2;

export type NestedBlocksState =
  | { kind: 'none'; blocks: [] }
  | { kind: 'editable'; blocks: PostBlock[] }
  | { kind: 'invalid'; blocks: unknown[] };

function isPostBlock(value: unknown): value is PostBlock {
  if (typeof value !== 'object' || value === null) return false;
  const block = value as Record<string, unknown>;
  return typeof block.id === 'string' && block.id.trim().length > 0
    && typeof block.type === 'string' && block.type.trim().length > 0;
}

export function nestedBlocksState(value: unknown): NestedBlocksState {
  if (!Array.isArray(value) || value.length === 0) return { kind: 'none', blocks: [] };
  if (!value.every(isPostBlock)) return { kind: 'invalid', blocks: value };
  return { kind: 'editable', blocks: value };
}

export function canAuthorNestedBlocks(depth: number): boolean {
  return Number.isInteger(depth) && depth >= 0 && depth < MAX_STUDIO_NESTING_DEPTH;
}

export function initialNestedBlocks(id: string): PostBlock[] {
  return [{ id, type: 'paragraph', content: '' }];
}

export function patchNestedBlock(
  blocks: PostBlock[],
  index: number,
  patch: Partial<PostBlock>,
): PostBlock[] {
  if (index < 0 || index >= blocks.length) return blocks.map((block) => structuredClone(block));
  return blocks.map((block, current) =>
    current === index ? { ...structuredClone(block), ...structuredClone(patch) } : structuredClone(block),
  );
}

export function moveNestedBlock(
  blocks: PostBlock[],
  index: number,
  direction: -1 | 1,
): PostBlock[] {
  const target = index + direction;
  const next = blocks.map((block) => structuredClone(block));
  if (index < 0 || index >= next.length || target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function removeNestedBlock(blocks: PostBlock[], index: number): PostBlock[] {
  if (blocks.length <= 1 || index < 0 || index >= blocks.length) {
    return blocks.map((block) => structuredClone(block));
  }
  return blocks.filter((_, current) => current !== index).map((block) => structuredClone(block));
}
