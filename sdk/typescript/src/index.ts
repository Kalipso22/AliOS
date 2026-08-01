export interface AliOSClientOptions { baseUrl: string; }

export class AliOSClient {
  public constructor(private readonly options: AliOSClientOptions) {}

  public healthUrl(): string { return new URL('/health', this.options.baseUrl).toString(); }
}
