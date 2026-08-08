import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { App } from './app';
import { Auth } from './auth/auth';

describe('App', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        // The shell renders routerLink directives, which need a router.
        provideRouter([]),
        // The shell asks who is signed in on startup. Testing backend, so
        // that request is captured rather than made.
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('renders the primary navigation once signed in', () => {
    TestBed.inject(Auth).status.set({
      configured: true,
      authenticated: true,
      username: 'ardome',
      has_passkeys: false,
    });

    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    const links = (fixture.nativeElement as HTMLElement).querySelectorAll(
      'nav a',
    );
    expect(Array.from(links).map((a) => a.textContent?.trim())).toEqual([
      'Month',
      'Transactions',
      'Linked',
      'Import',
      'Accounts',
      'Settings',
    ]);
  });

  it('hides the navigation when signed out', () => {
    TestBed.inject(Auth).status.set({
      configured: true,
      authenticated: false,
      username: null,
      has_passkeys: false,
    });

    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    const nav = (fixture.nativeElement as HTMLElement).querySelector('nav');
    expect(nav).toBeNull();
  });
});
