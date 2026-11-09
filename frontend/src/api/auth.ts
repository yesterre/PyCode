import { request } from './client'
import type { User } from './types'

export type Credentials = {account_name: string; password: string}
export const register = (body: Credentials) => request<User>('/auth/register', {method: 'POST', body: JSON.stringify(body)})
export const login = (body: Credentials) => request<{access_token: string; token_type: 'bearer'; expires_in: number}>(
  '/auth/login', {method: 'POST', body: JSON.stringify(body)})
export const getMe = () => request<User>('/auth/me')
