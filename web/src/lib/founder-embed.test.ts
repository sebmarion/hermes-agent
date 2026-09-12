import { describe, expect, it } from 'vitest';
import { isZeusFounderChat } from './founder-embed';
describe('founder presentation scope', () => {
 it('requires the chat route, explicit embed request, and intended profile together', () => {
  expect(isZeusFounderChat('/chat', '?embed=zeus&profile=zeus-os')).toBe(true);
  expect(isZeusFounderChat('/chat/', '?resume=abc&profile=zeus-os&embed=zeus')).toBe(true);
  for (const [route, query] of [['/config','?embed=zeus&profile=zeus-os'],['/chat','?embed=zeus&profile=other'],['/chat','?profile=zeus-os'],['/chat','?embed=other&profile=zeus-os']]) expect(isZeusFounderChat(route,query)).toBe(false);
 });
 it('changes with the current router query rather than cached global location', () => {
  const route='/chat';
  expect(isZeusFounderChat(route,'?embed=zeus&profile=zeus-os')).toBe(true);
  expect(isZeusFounderChat(route,'?profile=zeus-os')).toBe(false);
  expect(isZeusFounderChat(route,'?embed=zeus&profile=other')).toBe(false);
 });
});
